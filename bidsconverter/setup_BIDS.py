from __future__ import print_function

import json
import os
import os.path as op
import shutil
import fnmatch
import gzip
import nibabel as nib
import numpy as np
import warnings
from collections import OrderedDict
from copy import copy, deepcopy
from glob import glob
from joblib import Parallel, delayed
from .raw2nifti import parrec2nii
from .behav2tsv import Pres2tsv
from .physio import convert_phy
from .utils import check_executable, append_to_json


class BIDSConstructor(object):
    """
    Object to convert datasets to BIDS format.

    Attributes
    ----------
    project_dir : str
        Path to project directory
    cfg_file : str
        Path to config-file.

    Methods
    -------
    convert2bids()
        Initialize renaming and conversion project to BIDS-format.

    References
    ----------

    """

    def __init__(self, project_dir, cfg_file):
        """ Initializes a BIDSConstructor object.

        Parameters
        ----------
        project_dir : str
            Path to project directory
        cfg_file : str
            Path to config-file.
        """

        self.project_dir = project_dir
        self.cfg = None
        self._cfg_file = cfg_file
        self._dcm2niix = check_executable('dcm2niix')
        self._parrec2nii = check_executable('parrec2nii')
        self._edf2asc = check_executable('edf2asc')
        self._sub_dirs = None
        self._mappings = None
        self._debug = None

        if not self._dcm2niix:
            msg = "The program 'dcm2niix' was not found on this computer; install from neurodebian repository with "" \
                  ""'apt-get install dcm2niix', otherwise we can't convert par/rec (or dicom) to nifti!"
            warnings.warn(msg)

    def convert2bids(self):
        """ Method to call conversion process. """

        self._parse_cfg_file()
        self._sub_dirs = sorted(glob(op.join(self.project_dir, '%s*' % self.cfg['options']['subject_stem'])))
        if not self._sub_dirs:
            msg = "Could not find subdirs in %s." % self.project_dir
            raise ValueError(msg)

        for sub_dir in self._sub_dirs:

            sub_name = op.basename(sub_dir)
            print("Processing %s" % sub_name)

            out_dir = self.cfg['options']['out_dir']

            # Important: to find session-dirs, they should be named
            # ses-something
            sess_dirs = glob(op.join(sub_dir, 'ses-*'))

            if not sess_dirs:
                # If there are no session dirs, use sub_dir
                cdirs = [sub_dir]
            else:
                cdirs = sess_dirs

            DTYPES = ['func', 'anat', 'dwi', 'fmap']

            for cdir in cdirs:

                if 'ses-' in op.basename(cdir):
                    out_dir = op.join(out_dir, op.basename(cdir))
                else:
                    if not 'sub-' in sub_name:
                        nr = sub_name.split(self.cfg['options']['subject_stem'])[-1]
                        nr = nr.replace('-', '')  # remove unnecessary delimiters
                        nr = nr.replace('_', '')
                        sub_name = 'sub-' + nr
                    out_dir = op.join(out_dir, sub_name)
                    
                overwrite = self.cfg['options']['overwrite']
                already_exists = op.isdir(out_dir)

                if already_exists and not overwrite:
                    print('%s already converted - skipping ...' % out_dir)
                    continue

                # If it doesn't exist yet, start conversion
                data_types = [c for c in self.cfg.keys() if c in DTYPES]
                data_dir = [self._move_and_rename(cdir, dtype, sub_name) for dtype in data_types]
                dtype_dirs = [self._transform(data_dir[0], dtype) for dtype in data_types]

                if len(dtype_dirs) > 0:
                    self._extract_metadata(op.dirname(dtype_dirs[0]))

    def _parse_cfg_file(self):
        """ Parses config file and sets defaults. """

        dir_mappings = {'x': 'i', 'y': 'j', 'z': 'k',
                        'x-': 'i-', 'y-': 'j-', 'z-': 'k-'}

        if not op.isfile(self._cfg_file):
            msg = "Couldn't find config-file: %s" % self._cfg_file
            raise IOError(msg)

        with open(self._cfg_file) as config:
            self.cfg = json.load(config, object_pairs_hook=OrderedDict)

        options = self.cfg['options'].keys()
        if not 'mri_type' in options:
            self.cfg['options']['mri_type'] = 'parrec'

        if not 'n_cores' in options:
            self.cfg['options']['n_cores'] = -1

        if not 'subject_stem' in options:
            self.cfg['options']['subject_stem'] = 'sub'

        if not 'out_dir' in options:
            self.cfg['options']['out_dir'] = op.join(self.project_dir, 'bids_converted')
        else:
            self.cfg['options']['out_dir'] = op.join(self.project_dir, self.cfg['options']['out_dir'])

        if not 'overwrite' in options:
            self.cfg['options']['overwrite'] = False

        if not 'slice_order' in options:
            self.cfg['options']['slice_order'] = 'k'
        else:
            if options['slice_order'] in dir_mappings.keys():
                self.cfg['options']['slice_order'] = dir_mappings[options['slice_order']]

        if not 'SENSE_factor' in options:
            # Standard at Spinoza Centre REC-L
            self.cfg['options']['SENSE_factor'] = 2

        if not 'te_diff' in options:
            # Standard at Spinoza Centre REC-L
            self.cfg['options']['te_diff'] = 0.005
        else:
            if self.cfg['options']['te_diff'] >= 1:
                # probably in msec -> convert to sec
                self.cfg['options']['te_diff'] /= 1000

        if not 'pe_dir' in options:
            self.cfg['options']['pe_dir'] = 'j'
        else:
            if self.cfg['options']['pe_dir'] in dir_mappings.keys():
                self.cfg['options']['pe_dir'] = dir_mappings[self.cfg['options']['pe_dir']]

        for ftype in ['bold', 'T1w', 'dwi', 'physio', 'events', 'B0', 'eyedata']:
            if ftype not in self.cfg['mappings'].keys():
                self.cfg['mappings'][ftype] = None

        # Set attributes for readability
        self._mappings = self.cfg['mappings']
        self._debug = self.cfg['options']['debug']
        self._out_dir = self.cfg['options']['out_dir']

    def _move_and_rename(self, cdir, dtype, sub_name):
        """ Does the actual work of processing/renaming/conversion. """

        if not 'sub-' in sub_name:
            nr = sub_name.split(self.cfg['options']['subject_stem'])[-1]
            nr = nr.replace('-', '')  # remove unnecessary delimiters
            nr = nr.replace('_', '')
            sub_name = 'sub-' + nr

        n_elem = len(self.cfg[dtype])

        if n_elem == 0:
            return 0

        unallocated = []
        # Loop over contents of func/anat/dwi/fieldmap
        for elem in self.cfg[dtype].keys():

            # Kinda ugly, but can't figure out a more elegant way atm
            kv_pairs = deepcopy(self.cfg[dtype][elem])
            idf = deepcopy(kv_pairs['id'])
            del kv_pairs['id']

            # common_name is simply sub-xxx
            common_name = copy(sub_name)

            # Add session-id pair to name if there are sessions!
            if 'ses-' in op.basename(cdir):
                sess_id = op.basename(cdir).split('ses-')[-1]
                common_name += '_%s-%s' % ('ses', sess_id)

            for key, value in kv_pairs.items():

                # Append key-value pair if it's not an empty string
                if value and key != 'mapping':
                    common_name += '_%s-%s' % (key, value)
                elif key == 'mapping':
                    common_name += '_%s' % value

            # Find files corresponding to func/anat/dwi/fieldmap
            files = [f for f in glob(op.join(cdir, '*%s*' % idf)) if op.isfile(f)]

            if not files:  # check one level lower
                files = [f for f in glob(op.join(cdir, '*', '*%s*' % idf)) if op.isfile(f)]

            if files:
                if 'ses-' in op.basename(cdir):
                    data_dir = self._make_dir(op.join(self._out_dir, sub_name,
                                                      op.basename(cdir),
                                                      dtype))
                else:
                    data_dir = self._make_dir(op.join(self._out_dir, sub_name,
                                                      dtype))
            else:
                data_dir = op.join(self._out_dir, sub_name, dtype)

            for f in files:
                # Rename files according to mapping
                types = []
                for ftype, match in self._mappings.items():
                    # If e.g. no eyedata mapping, just skip it
                    # This should be refactored!
                    if match is None:
                        continue

                    match = '*' + match + '*'
                    if fnmatch.fnmatch(op.basename(f), match):
                        types.append(ftype)

                if len(types) > 1:
                    msg = "Couldn't determine file-type for file '%s'; is one of the "\
                          "following:\n %r" % (f, types)
                    warnings.warn(msg)
                elif len(types) == 1:
                    filetype = types[0]
                else:
                    unallocated.append(f)
                    # No file found; ends up in unallocated (printed later).
                    continue

                # Create full name as common_name + unique filetype + original extension
                exts = f.split('.')[1:]

                # For some weird reason, people seem to use periods in filenames,
                # so remove all unnecessary 'extensions'
                allowed_exts = ['par', 'rec', 'nii', 'gz', 'dcm', 'pickle',
                                'json', 'edf', 'log', 'bz2', 'tar', 'phy',
                                'cPickle', 'pkl', 'jl', 'tsv', 'csv']

                upper_exts = [s.upper() for s in allowed_exts]
                allowed_exts.extend(upper_exts)

                clean_exts = '.'.join([e for e in exts if e in allowed_exts])
                full_name = op.join(data_dir, common_name + '_%s.%s' %
                                    (filetype, clean_exts))
                full_name = full_name.replace('_b0', '')
                full_name = full_name.replace('_B0', '')

                if self._debug:
                    print("Renaming '%s' as '%s'" % (f, full_name))

                if not op.isfile(full_name):
                    shutil.copyfile(f, full_name)

        if unallocated:
            print('Unallocated files for %s:' % sub_name)
            print('\n'.join(unallocated))

        return op.dirname(data_dir)

    def _transform(self, sess_dir, dtype):
        """ Transforms files to appropriate format (nii.gz or tsv). """

        data_dir = op.join(sess_dir, dtype)
        self._mri2nifti(data_dir, n_cores=self.cfg['options']['n_cores'])
        self._log2tsv(data_dir, type=self.cfg['options']['log_type'])

        if self._mappings['eyedata'] is not None:
            self._edf2tsv(data_dir)

        if self._mappings['physio'] is not None:
            self._phys2tsv(data_dir, n_cores=self.cfg['options']['n_cores'])

        # Move topup files to fmap directory
        topups = glob(op.join(data_dir, '*_topup*'))

        if topups and dtype != 'fmap':
            dest = self._make_dir(op.join(sess_dir, 'fmap'))
            [shutil.move(tu, dest) for tu in topups]
        return data_dir

    def _extract_metadata(self, sdir):

        # Append TaskName
        func_jsons = sorted(glob(op.join(sdir, '*', '*task-*bold.json')))
        task_names = [op.basename(j).split('task-')[1].split('_')[0] for j in func_jsons]
        _ = [append_to_json(j, {'TaskName': taskn})
             for j, taskn in zip(func_jsons, task_names)]

        # Append slicetime info and other stuff
        func_files = sorted(glob(op.join(sdir, '*', '*_bold.nii.gz')))
        TRs = [nib.load(f).header['pixdim'][4] for f in func_files]
        n_slices = [nib.load(f).header['dim'][3] for f in func_files]
        slice_times = [np.linspace(0, TR, n_slice) for TR, n_slice
                       in zip(TRs, n_slices)]

        _ = [append_to_json(j, {'SliceTiming': slice_time.tolist(),
                                'PhaseEncodingDirection': self.cfg['options']['pe_dir'],
                                'SliceEncodingDirection': self.cfg['options']['slice_order']})
             for j, slice_time in zip(func_jsons, slice_times)]

        # Append IntendedFor info to B0 jsons
        fmap_jsons = sorted(glob(op.join(sdir, 'fmap', '*.json')))
        to_append = {'IntendedFor': ['func/%s' % op.basename(f) for f in func_files]}
        _ = [append_to_json(j, to_append) for j in fmap_jsons]

    def _compress(self, f):

        with open(f, 'rb') as f_in, gzip.open(f + '.gz', 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(f)

    def _mri2nifti(self, directory, compress=True, n_cores=-1):
        """ Converts raw mri to nifti.gz. """

        compress = False if self._debug else True
        te_diff = self.cfg['options']['te_diff']
        acc = self.cfg['options']['SENSE_factor']

        if self.cfg['options']['mri_type'] == 'parrec':
            PAR_files = self._glob(directory, ['.PAR', '.par'])
            if PAR_files:
                epi_yes_no = [self._mappings['bold'] in str(p)
                              for p in PAR_files]
                Parallel(n_jobs=n_cores)(delayed(parrec2nii)(pfile, epi, acc, te_diff, compress)
                                         for pfile, epi in zip(PAR_files, epi_yes_no))

        elif self.cfg['options']['mri_type'] == 'nifti':
            niftis = self._glob(directory, ['.nii', '.nifti'])

            if niftis:
                _ = [self._compress(f) for f in niftis]

        elif self.cfg['options']['mri_type'] == 'nifti-gz':
            pass

        elif self.cfg['options']['mri_type'] == 'dicom':
            print('DICOM conversion not yet implemented!')
        else:
            print("'%s' conversion not yet supported!" % self.cfg['options']['mri_type'])

        # Check for left-over unconverted niftis
        if compress:

            niftis = self._glob(directory, ['.nii', '.nifti'])

            if niftis:
                _ = [self._compress(f) for f in niftis]

    def _log2tsv(self, directory, type='Presentation'):
        """ Converts behavioral logs to event_files. """

        if type == 'Presentation':
            logs = glob(op.join(directory, '*.log'))
            event_dir = op.join(self.project_dir, 'task_info')

            if not op.isdir(event_dir):
                raise IOError("The event_dir '%s' doesnt exist!" % event_dir)

            for log in logs:
                plc = Pres2tsv(in_file=log, event_dir=event_dir)
                plc.parse()
        else:
            warnings.warn("Conversion of logfiles other than type='Presentation'"
                          " is not yet supported.")

    def _edf2tsv(self, directory):

        idf = self.cfg['mappings']['eyedata']
        if idf:
            edfs = glob(op.join(directory, '*%s*' % idf))

            if edfs:
                # Yet to implement!
                pass

    def _phys2tsv(self, directory, n_cores=-1):

        idf = self.cfg['mappings']['physio']
        phys = glob(op.join(directory, '*%s*' % idf))

        if phys:
            Parallel(n_jobs=n_cores)(delayed(convert_phy)(f) for f in phys)

    def _make_dir(self, path):
        """ Creates dir-if-not-exists-already. """
        if not op.isdir(path):
            os.makedirs(path)

        return path

    def _glob(self, path, wildcards):

        files = []

        for w in wildcards:
            files.extend(glob(op.join(path, '*%s' % w)))

        return files
