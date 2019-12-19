import collections
import logging
import math
import os
import re
import shutil
import sys
from monty.io import zopen
import numpy as np
from pymatgen.core.lattice import Lattice
from pymatgen.core.structure import Structure
from pymatgen.electronic_structure.core import Spin
from pymatgen.electronic_structure.bandstructure import BandStructureSymmLine
from pymatgen.electronic_structure.dos import Dos
from pymatgen.phonon.bandstructure import PhononBandStructureSymmLine

_bohr_to_angstrom = 0.5291772
_ry_to_ev = 13.605693009

# From 2002 CODATA values
to_angstrom = {'ang': 1,
               'bohr': 0.5291772108,
               'm': 1e10,
               'cm': 1e8,
               'nm': 10}

Tag = collections.namedtuple('Tag', 'value comment')
Block = collections.namedtuple('Block', 'values comments')

unsupported_dosplot_args = {'elements', 'lm_orbitals', 'atoms'}

class CastepCell(object):
    """Structure information and more: CASTEP seedname.cell file

    Usually this will be instantiated with the
    :obj:`sumo.io.castep.CastepCell.from_file()` method

    Keys should always be lower-case; values will retain their case.

    Tags are stored as a dict, where each key is a tag and each value is a
    Tag NamedTuple with the attributes 'value' and 'comment'.

    Blocks are also stored as a dict, where each key is the block name
    (e.g. 'bs_kpoint_list') and the value is a Block NamedTuple with attributes
    'values' (a list of lists) and 'comments' (a commensurate list of str).

    """

    def __init__(self, blocks=None, tags=None):
        self.blocks = blocks if blocks else {}
        self.tags = tags if tags else {}

    @property
    def structure(self):
        # Get lattice vectors
        if 'lattice_cart' in self.blocks:
            lattice_cart = self.blocks['lattice_cart'].values
            if lattice_cart[0][0].lower() in ('ang', 'nm', 'cm',
                                              'm', 'bohr', 'a0'):
                unit = lattice_cart[0][0].lower()
                vectors = lattice_cart[1:]
            else:
                unit = 'ang'
                vectors = lattice_cart
            vectors = np.array([list(map(float, row)) for row in vectors])
            if vectors.shape != (3, 3):
                raise ValueError('lattice_cart should contain a 3x3 matrix')

            vectors *= to_angstrom[unit]
            lattice = Lattice(vectors)
        elif 'lattice_abc' in self.blocks:
            lattice_abc = self.blocks['lattice_abc'].values
            if lattice_abc[0][0].lower() in ('ang', 'nm', 'cm', 'm',
                                             'bohr', 'a0'):
                unit = lattice_abc[0][0].lower()
                lengths_and_angles = lattice_abc[1:]
            else:
                unit = 'ang'
                lengths_and_angles = lattice_abc[1:]
            if len(lengths_and_angles) != 2:
                raise ValueError('lattice_abc should have two rows')
            lengths_and_angles = [list(map(float, row))
                                  for row in lengths_and_angles]
            lengths_and_angles[0] = [x * to_angstrom[unit]
                                     for x in lengths_and_angles[0]]
            lattice = Lattice.from_lengths_and_angles(*lengths_and_angles)
        else:
            raise ValueError("Couldn't find a lattice in cell file")

        if 'positions_frac' in self.blocks:
            elements_coords = [(row[0], list(map(float, row[1:4])))
                               for row in self.blocks['positions_frac']]
            elements, coords = zip(*elements_coords)
            return Structure(lattice, elements, coords,
                             coords_are_cartesian=False)
        elif 'positions_abs' in self.blocks:
            positions_abs = self.blocks['positions_abs'].values
            if positions_abs[0][0].lower() in ('ang', 'nm', 'cm', 'm',
                                               'bohr', 'a0'):
                unit = positions_abs[0][0].lower()
                positions_abs = positions_abs[1:]
            else:
                unit = 'ang'
            elements_coords = [(row[0], list(map(float, row[1:4])))
                               for row in positions_abs]
            elements, coords = zip(*elements_coords)
            return Structure(lattice, elements, coords,
                             coords_are_cartesian=True)
        else:
            raise ValueError("Couldn't find any atom positions in cell file")

    def to_file(self, filename):
        with open(filename, 'wt') as f:
            for tag, content in self.tags.items():
                if content.comment in (None, ''):
                    f.write('{0: <24}: {1}\n'.format(tag,
                                                     ' '.join(content.value)))
                else:
                    f.write('{0: <24}: {1: <16} ! {2}\n'.format(
                        tag, ' '.join(content.value), content.comment))
            for block, content in self.blocks.items():
                f.write('\n%block {}\n'.format(block))
                if content.comments is None:
                    comments = [''] * len(content.values)
                else:
                    comments = content.comments

                for row, comment in zip(content.values, comments):
                    line = ' '.join(map(str, row))
                    if comment != '':
                        line = '{0: <30} ! {1}'.format(line, comment)
                    line = line + '\n'
                    f.write(line)

                f.write('%endblock {}\n'.format(block))

    @classmethod
    def from_file(cls, filename):

        with zopen(filename, 'rt') as f:
            lines = [line.strip() for line in f]

        # Remove lines which are entirely commented or empty
        def _is_not_empty_line(line):
            if len(line) == 0:
                return False
            elif line[0] in '#;!':
                return False
            elif len(line) > 6 and line[:7] == 'COMMENT':
                return False
            else:
                return True

        lines = list(filter(_is_not_empty_line, lines))

        tags, blocks = {}, {}
        current_block_values, current_block_comments = [], []
        in_block = False
        current_block_label = ''

        for line in lines:
            if len(line.split()) == 0:
                continue
            elif line[:6].lower() == '%block':
                if in_block:
                    raise IOError('Cell file contains nested blocks. '
                                  'This possibility was not anticipated.')
                else:
                    current_block_label = line.split()[1].lower()
                    in_block = True
                    continue
            elif line[:9].lower() == '%endblock':
                if line.split()[1].lower() != current_block_label:
                    raise IOError('Endblock {} does not match current block '
                                  '{}: cannot interpret cell file.'.format(
                                      line.split()[1], current_block_label))
                if in_block:
                    blocks[current_block_label] = Block(current_block_values,
                                                        current_block_comments)

                    current_block_values, current_block_comments = [], []
                    in_block = False
                else:
                    raise IOError('Cannot cope with line {}: not currently in '
                                  'a block.'.format(line))
            elif in_block:
                _, data, comment = _data_comment_from_line(line, in_block=True)
                current_block_values.append(data)
                current_block_comments.append(comment)

            else:
                tag, data, comment = _data_comment_from_line(line)
                tags[tag.lower()] = Tag(data, comment)

        return cls(tags=tags, blocks=blocks)


def read_tdos(bands_file, bin_width=0.01, gaussian=None,
              padding=0.5, emin=None, emax=None):
    """Convert DOS data from CASTEP .bands file to Pymatgen/Sumo format

    The data is binned into a regular series using np.histogram

    Args:
        bands_file (:obj:`str`): Path to CASTEP prefix.bands output file. The
            k-point positions, weights and eigenvalues are read from this file.
        bin_width (:obj:`float`, optional): Spacing for DOS energy axis
        gaussian (:obj:`float` or None, optional): Width of Gaussian broadening
            function
        padding (:obj:`float`, optional): Energy range above and below occupied
            region. (This is not used if xmin and xmax are set.)
        emin (:obj:`float`, optional): Minimum energy value for output DOS
        emax (:obj:`float`, optional): Maximum energy value for output DOS

    Returns:
        :obj:`pymatgen.electronic_structure.dos.Dos`
    """

    header = _read_bands_header_verbose(bands_file)

    logging.info("Reading band eigenvalues...")
    _, weights, eigenvalues = read_bands_eigenvalues(bands_file, header)

    emin_data = min(eigenvalues[Spin.up].flatten())
    emax_data = max(eigenvalues[Spin.up].flatten())
    if Spin.down in eigenvalues:
        emin_data = min(emin_data, min(eigenvalues[Spin.down].flatten()))
        emax_data = max(emax_data, max(eigenvalues[Spin.down].flatten()))

    if emin is None:
        emin = emin_data - padding
    if emax is None:
        emax = emax_data + padding

    bins = np.arange(emin, emax + bin_width, bin_width)
    energies = (bins[1:] + bins[:-1]) / 2

    # Add rows to weights for each band so they are aligned with eigenval data
    weights = weights * np.ones([eigenvalues[Spin.up].shape[0], 1])

    dos_data = {spin: np.histogram(eigenvalue_set, bins=bins,
                                   weights=weights)[0]
                for spin, eigenvalue_set in eigenvalues.items()}

    dos = Dos(header['e_fermi'][0], energies, dos_data)
    if gaussian:
        dos.densities = dos.get_smeared_densities(gaussian)

    return dos


def band_structure(bands_file, cell_file=None):
    """Convert band structure data from CASTEP to Pymatgen/Sumo format

    Args:
        bands_file (:obj:`str`): Path to CASTEP prefix.bands output file. The
            lattice parameters, k-point positions and eigenvalues are read from
            this file.
        cell_file (:obj:`str`, optional): Path to CASTEP prefix.cell input
            file. If found, the positions of high-symmetry points are read from
            the ``bs_kpoint_path`` block in this file.

    Returns:
        :obj:`pymatgen.electronic_structure.bandstructure.BandStructureSymmLine`

    """

    header = _read_bands_header_verbose(bands_file)

    lattice = Lattice(header['lattice_vectors'])
    lattice = Lattice(lattice.matrix * _bohr_to_angstrom)

    logging.info("Reading band structure eigenvalues...")
    kpoints, _, eigenvalues = read_bands_eigenvalues(bands_file, header)

    if cell_file is None:
        labels = {}
    else:
        labels = labels_from_cell(cell_file)

    return BandStructureSymmLine(kpoints, eigenvalues,
                                 lattice.reciprocal_lattice_crystallographic,
                                 header['e_fermi'][0] * _ry_to_ev * 2,
                                 labels,
                                 coords_are_cartesian=False)


def labels_from_cell(cell_file, phonon=False):
    """Read special k-point positions/labels from CASTEP cell file

    Args:
        cell_file (:obj:`str`): Path to CASTEP prefix.cell input file
            high-symmetry points will be read from a block beginning with

                %block bs_kpoint_path

            and ending with

                %endblock bs_kpoint path

            where each special-point line is formatted

                k1 k2 k3  ! label

            where k1, k2, k3 are given in fractional coordinates of the
            reciprocal lattice.
        phonon (:obj:`bool`): Look for phonon interpolation  blocks instead of
            electronic band structure (e.g. PHONON_FINE_KPOINT_PATH)

    returns:
        {:obj:`str`: :obj:`tuple`, ...}

    """

    labels = {}

    if phonon:
        blockstart = re.compile(
            r'^%block\s+phonon_fine_kpoint(s)?_(path|list)')
        blockend = re.compile(
            r'^%endblock\s+phonon_fine_kpoint(s)?_(path|list)')
    else:
        blockstart = re.compile(r'^%block\s+bs_kpoint(s)?_(path|list)')
        blockend = re.compile(r'^%endblock\s+bs_kpoint(s)?_(path|list)')

    with zopen(cell_file, 'r') as f:
        line = ''
        while blockstart.match(line.lower()) is None:
            line = f.readline()
            if line == '':
                logging.error("Could not find start of k-point block in "
                              "cell file.")
                sys.exit()

        line = f.readline()  # Skip past block start line
        while blockend.match(line.lower()) is None:
            kpt = tuple(map(float, line.split()[:3]))
            if len(line.split()) > 3:
                label = line.split()[-1]
                if label.lower() in ('g', 'gamma'):
                    label = r'\Gamma'
                labels[label] = kpt
            line = f.readline()
            if line == '':
                logging.error("Could not find end of k-point block in "
                              "cell file.")
                sys.exit()

    return labels


def _read_bands_header_verbose(bands_file):
    logging.info("Reading CASTEP .bands file header...")
    header = read_bands_header(bands_file)

    if header['n_spins'] == 1:
        logging.info("nbands: {}, efermi / Ry: {}, spin channels: 1".format(
            header['n_bands'][0],
            header['e_fermi'][0]))
    elif header['n_spins'] == 2:
        logging.info("nbands: {}, efermi / Ry: {}, spin channels: 2".format(
            header['n_bands'],
            header['e_fermi']))
    else:
        raise ValueError('Should only be 1 or 2 spin channels!')

    if len(header['e_fermi']) == 2:
        if abs(header['e_fermi'][1] - header['e_fermi'][1]) > 1e-8:
            raise NotImplementedError("Different Fermi energy in each channel."
                                      " I have no idea how to handle that.")
    return header


def read_bands_header(bands_file):
    """Read CASTEP bands file header, get lattice vectors and metadata

    Args:
        bands_file (:obj:`str`): Path to CASTEP prefix.bands output file

    Returns:
        :obj:`dict`
            CASTEP bands metadata of the form::

            {'lattice': [[ax, ay, az], [bx, by, bz], [cx, cy, cz]],
             'n_kpoints': n_kpoints,
             'n_spins': n_spins,
             'n_electrons': [n_spin1, n_spin2],
             'n_bands': [n_bands_spin1, n_bands_spin2],
             'e_fermi': [e_fermi_spin1, e_fermi_spin2]}

    """

    header = {}
    with zopen(bands_file, 'r') as f:
        # Read in header information a line at a time

        header['n_kpoints'] = int(f.readline().split()[-1])
        header['n_spins'] = int(f.readline().split()[-1])
        header['n_electrons'] = [float(x) for x in f.readline().split()[3:]]
        header['n_bands'] = [int(x) for x in f.readline().split()[3:]]

        fermi_line = f.readline().split()
        if fermi_line[3] != 'atomic':
            raise NotImplementedError('Band data not in atomic units')
        header['e_fermi'] = [float(x) for x in fermi_line[5:]]

        vectors_header = f.readline().strip()
        if vectors_header != 'Unit cell vectors':
            raise AssertionError('CASTEP bands file not formatted as expected',
                                 vectors_header)

        lattice_vectors = [[float(x) for x in f.readline().split()]
                           for _ in range(3)]
        for row in lattice_vectors:
            if len(row) != 3:
                raise AssertionError('Unit cell vectors not read correctly')
        header['lattice_vectors'] = lattice_vectors

    return header


def read_bands_eigenvalues(bands_file, header):
    """Read eigenvalue data from a CASTEP bands file

    Args:
        bands_file (:obj:`str`): Path to CASTEP prefix.bands output file
        header (:obj:`dict`): Band metadata, as obtained by reading the bands
            file with :obj:`read_bands_header`.

    Returns:
        (:obj:`list`, :obj:`list`, :obj:`np.ndarray`)
            kpoints list, weights and eigenvalue array ordered by
            [band][kpt]. Each k-point is expressed as numpy array of length
            3. Eigenvalues are converted from Ry to eV.

    """

    # Implementation note: pymatgen band structure expects an eigenvalue array
    # ordered [band][kpt] but the data will be read in one kpt at a time as
    # this is the format of the bnd data file.
    #
    # We will build a nested list [[bnd1_kpt1, bnd2_kpt1, ...],
    #                              [bnd1_kpt2, bnd2_kpt2, ...], ...]
    # then convert to a numpy array (i.e. with each row for a kpoint)
    # and then transpose the array to obtain desired formats
    #
    # CASTEP can write the k-points out-of-order, so we need to keep track of
    # the k-point indices and re-order the lists before array conversion.

    kpoints = []
    if header['n_spins'] == 1:
        eigenvals = {Spin.up: []}
    else:
        eigenvals = {Spin.up: [], Spin.down: []}

    with zopen(bands_file, 'r') as f:
        for _ in range(9):
            f.readline()  # Skip header

        for _ in range(header['n_kpoints']):
            # The first "k-point" column is actually the sorting index
            # and the last is the weighting
            kpoints.append(np.array(
                [float(x) for x in f.readline().split()[1:]]))
            f.readline()  # Skip past "Spin component 1"
            spin1_bands = ([kpoints[-1][0]] +     # Sorting key goes in col 0
                           [float(f.readline())
                            for _i in range(header['n_bands'][0])])
            eigenvals[Spin.up].append(spin1_bands)

            if header['n_spins'] == 2:
                f.readline()  # Skip past "Spin component 2"
                spin2_bands = ([kpoints[-1][0]] +  # Sorting key goes in col 0
                               [float(f.readline())
                                for _i in range(header['n_bands'][0])])
                eigenvals[Spin.down].append(spin2_bands)

    # Sort kpoints and trim off sort keys, weights
    kpoint_list = sorted(kpoints, key=(lambda k: k[0]))
    kpoints = [k[1:4] for k in kpoint_list]
    weights = [k[-1] for k in kpoint_list]

    # Sort eigenvalues and trim off sort keys
    for key, data in eigenvals.items():
        data = np.array(data)
        eigenvals[key] = data[data[:, 0].argsort(), 1:]
    # Transpose matrix to arrange by band and convert to eV from Ha
    eigenvals = {key: data.T * _ry_to_ev * 2
                 for key, data in eigenvals.items()}

    return kpoints, weights, eigenvals


def write_kpoint_files(filename, kpoints, labels, make_folders=False,
                       kpts_per_split=None, phonon=False, directory=None):
    r"""Write the k-points data to files.

    Folders are named as 'split-01', 'split-02', etc ...
    .cell files are named band_split_01.cell etc ...

    Cartesian coordinates are not supported by CASTEP for band paths.

    Unlike VASP, CASTEP band structure calculations can deal with two
    separate k-point meshes: a mesh for the SCF convergence and a series of
    points (or path segments) for the band structure calculation. It is assumed
    that the existing .cell file contains a specification of the SCF k-point
    mesh, and Sumo will append the band path information.

    Args:
        filename (:obj:`str`): Path to CASTEP structure (.cell) file.
        kpoints (:obj:`numpy.ndarray`): The k-point coordinates along the
            high-symmetry path. For example::

                [[0, 0, 0], [0.25, 0, 0], [0.5, 0, 0], [0.5, 0, 0.25],
                [0.5, 0, 0.5]]

        labels (:obj:`list`) The high symmetry labels for each k-point (will be
            an empty :obj:`str` if the k-point has no label). For example::

                ['\Gamma', '', 'X', '', 'Y']

        make_folders (:obj:`bool`, optional): Generate folders and copy in
            param file from the current directory, setting task=BandStructure.
            Cell file filename will be copies with the new band path block.

        kpts_per_split (:obj:`int`, optional): If set, the k-points are split
            into separate k-point files (or folders) each containing the number
            of k-points specified. This is useful for hybrid band structure
            calculations where it is often intractable to calculate all
            k-points in the same calculation.

        phonon (:obj:`bool`, optional): Write the Brillouin-zone path as an
            interpolated phonon q-point path (PHONON_FINE_KPOINTS_PATH) instead
            of electronic band-structure k-points.

        directory (:obj:`str`, optional): The output file directory.
        """

    if kpts_per_split:
        kpt_splits = [kpoints[i:i+kpts_per_split] for
                      i in range(0, len(kpoints), kpts_per_split)]
        label_splits = [labels[i:i+kpts_per_split] for
                        i in range(0, len(labels), kpts_per_split)]
    else:
        kpt_splits = [kpoints]
        label_splits = [labels]

    kpt_cell_files = []

    if phonon:
        task = 'Phonon'
        kpoint_tag = 'phonon_fine_kpoint_list'
        clash_tags = {'phonon_fine_kpoints_list',
                      'phonon_fine_kpoint_path',
                      'phonon_fine_kpoints_path'}
    else:
        task = 'BandStructure'
        kpoint_tag = 'bs_kpoint_list'
        clash_tags = {'bs_kpoints_list', 'bs_kpoint_path', 'bs_kpoints_path'}

    for kpt_split, label_split in zip(kpt_splits, label_splits):
        cellfile = CastepCell.from_file(filename)
        cellfile.blocks[kpoint_tag] = Block(kpt_split, label_split)
        # Remove any other band structure blocks which may have been loaded

        for key in clash_tags:
            try:
                del cellfile.blocks[key]
            except KeyError:
                pass

        kpt_cell_files.append(cellfile)

    pad = int(math.floor(math.log10(len(kpt_cell_files)))) + 2
    if make_folders:
        # Derive param file name by changing extension from cell file
        param_file = '.'.join(filename.split('.')[:-1] + ['param'])

        for i, cell_file in enumerate(kpt_cell_files):
            split_folder = 'split-{}'.format(str(i + 1).zfill(pad))
            if directory:
                split_folder = os.path.join(directory, split_folder)

            copy_param(param_file, split_folder, task=task)

            cell_file.to_file(os.path.join(split_folder,
                                           os.path.basename(filename)))

    else:
        for i, cell_file in enumerate(kpt_cell_files):
            if len(kpt_cell_files) > 1:
                cell_filename = 'band_split_{:0d}.cell'.format(i + 1)
            else:
                cell_filename = 'band.cell'
            if directory:
                cell_filename = os.path.join(directory, cell_filename)
            cell_file.to_file(cell_filename)


def copy_param(filename, folder, task=None):
    """Copy CASTEP .param file, modifying Task if needed

    Args:
        filename (:obj:`str`): Path to CASTEP .param file
        folder (:obj:`str`): Directory for output .param file
        task (:obj:`str` or :obj:`None`): Replace value of existing Task tag

    """

    output_filename = os.path.join(folder, os.path.basename(filename))
    if not os.path.isdir(folder):
        os.mkdir(folder)
    if os.path.isfile(filename) and task is None:
        shutil.copyfile(filename, output_filename)

    elif os.path.isfile(filename):
        with open(filename, 'r') as infile:
            with open(output_filename, 'w') as outfile:
                for line in infile:
                    if line.strip().lower()[:4] == 'task':
                        outfile.write('Task : {}\n'.format(task))
                    else:
                        outfile.write(line)
    else:
        logging.warning("Cannot find param file {}, skipping".format(filename))


def _data_comment_from_line(line, in_block=False):
    """Parse a line from a CASTEP input file

    Args:
        line (str): line from CASTEP .cell or .param file
        in_block (bool): Include all tokens before comment in
            data and leave tag as None.

    Returns:
        (tag, data, comment) where tag is a str, data is a list
        of str and comment is a str

    """
    comment_split_line = re.split('[#;!]', line)

    line_content = comment_split_line[0]
    if len(comment_split_line) == 1:
        comment = ''
    else:
        comment = line[len(line_content):]

    if in_block:
        tag, data = None, line.split()
    else:
        tag, *data = line_content.split()
        if len(data) == 0:
            data = ['']

        # Clean up delimiter: 'TAG DAT' 'TAG= DAT' 'TAG : DAT'
        # are all possible so check both symbols in both places
        data = [item for item in data if item not in ':=']
        if tag[-1] in ':=':
            tag = tag[:-1]

    return tag, data, comment


class CastepPhonon(object):
    """Data from CASTEP phonon calculation: seedname.phonon file

    Usually this will be instantiated with the
    :obj:`sumo.io.castep.CastepPhonon.from_file()` method

    """
    def __init__(self, header, qpts, frequencies,
                 weights=None, eigenvectors=None):
        """
        Args:
            header (:obj:`dict`):
                Dict containing key metadata from header file. Positions are in
                fractional coordinates.

                  {nions: NIONS, nbranches: NBRANCHES, nqpts: NQPTS,
                   frequency_unit: 'cm-1',
                   cell: [[ax, ay, az], [bx, by, bz], [cx, cy, cz]],
                   positions: [[a1, b1, c1], [a2, b2, c2], ...],
                   symbols: [el1, el2, ...],
                   masses: [m1, m2, ...]}

            qpts (:obj:`numpy.ndarray`):
                Nx3 array of q-points in fractional coordinates
            frequencies (:obj:`numpy.ndarray`):
                2-D array of frequencies arranged [mode, qpt]. (Frequencies are
                in units of header['frequency_unit']).
            weights (:obj:`numpy.ndarray`, optional):
                1-D array of q-point weights (not used in band structure plot).
            eigenvectors (:obj:`numpy.ndarray`, optional):
                4-D array of phonon eigenvectors arranged
                [mode, qpt, atom, xyz] (not used in band structure plot).

        """
        self.header = header
        self.qpts = qpts
        self.weights = weights
        self.frequencies = frequencies
        self.eigenvectors = eigenvectors
        self.labels = {}

    @classmethod
    def from_file(cls, filename):
        """Create a CastepPhonon object by reading CASTEP .phonon file

        Args:
            filename (:obj:`str`): Input .phonon file

        Returns:
            pymatgen.phonon.bandstructure.PhononBandStructure
        """
        header = read_phonon_header(filename)
        qpts, weights, frequencies, eigenvectors = read_phonon_bands(filename,
                                                                     header)

        return cls(header, qpts, frequencies,
                   weights=weights, eigenvectors=eigenvectors)

    def set_labels_from_file(self, filename):
        """Set dictionary of special-point labels from .cell file comments

        Args:
            filename (:obj:`str`):
                Input .cell file. Special point symbols should be included as
                comments on the lines where those qpts are defined; *kgen* will
                do this automatically when writing high-symmetry paths.

        """
        self.labels = labels_from_cell(filename, phonon=True)


    def get_band_structure(self):
        lattice = Lattice(self.header['cell'])
        structure = Structure(lattice, species=self.header['symbols'],
                              coords=self.header['positions'])

        # I think this is right? Need to test somehow...
        mass_weights = 1 / np.sqrt(self.header['masses'])
        displacements = self.eigenvectors * mass_weights[np.newaxis,
                                                         np.newaxis,
                                                         :,
                                                         np.newaxis]

        return PhononBandStructureSymmLine(self.qpts, self.frequencies,
                                           lattice.reciprocal_lattice,
                                           structure=structure,
                                           eigendisplacements=displacements,
                                           labels_dict=self.labels)


def read_phonon_header(filename):
    """Read header information from CASTEP .phonon file

    Args:
        filename (:obj:`str`): Input file

    Returns:
        :obj:`dict`:

        Dict containing key metadata from header file. Positions are in
        fractional coordinates.

          {nions: NIONS, nbranches: NBRANCHES, nqpts: NQPTS,
           frequency_unit: 'cm-1',
           cell: [[ax, ay, az], [bx, by, bz], [cx, cy, cz]],
           positions: [[a1, b1, c1], [a2, b2, c2], ...],
           symbols: [el1, el2, ...],
           masses: [m1, m2, ...]}
    """

    int_keys = {'ions': 'nions',
                'branches': 'nbranches',
                'wavevectors': 'nqpts'}

    header = {}
    in_header = False
    with zopen(filename, 'rt') as f:
        for line in f:
            line = line.strip()
            if line[:12] == 'BEGIN header':
                in_header = True
            elif line[:10] == 'END header':
                break
            elif not in_header:
                pass
            else:
                split_line = line.split()
                # Get int data e.g. "Number of ions      1"
                if split_line[0] == 'Number':
                    header[int_keys[split_line[2]]] = int(split_line[3])

                # Lattice vector block: next() will skip lines from for-loop
                elif split_line[:3] == ['Unit', 'cell', 'vectors']:
                    logging.info("Reading unit cell...")
                    cell = [next(f).split(), next(f).split(), next(f).split()]
                    header['cell'] = np.array(cell, dtype=float).tolist()

                # Positions block: pre-allocate arrays and fill line-by-line
                elif split_line[:2] == ['Fractional', 'Co-ordinates']:
                    if not header.get('nions'):
                        raise IOError("Could not find number of ions; phonon "
                                      "file not formatted correctly.")

                    logging.info("Reading structure...")

                    header['positions'] = []
                    header['symbols'] = []
                    header['masses'] = []

                    for i in range(header['nions']):
                        _, *position, el, m = next(f).split()
                        header['positions'].append(list(map(float, position)))
                        header['symbols'].append(el)
                        header['masses'].append(float(m))
                else:
                    logging.debug(
                        'Skipping unused header line "{}"'.format(line))
    logging.info("Finished reading header")
    return header


def read_phonon_bands(filename, header):
    """Using header information, read band data from CASTEP .phonon file

    Args:
        filename (:obj:`str`): Input file
        header (:obj:`dict`): Header data including

          {'nions': NIONS, 'nbranches': NBRANCHES, 'nqpts': NQPTS, ...}

    Returns:
        :obj:`tuple` (qpts, weights, frequencies, eigenvectors)

        where *qpts* is a 3xN :obj:`numpy.ndarray` of q-points in fractional
        coordinates, *weights* is a 1-D :obj:`numpy.ndarray` of q-point
        weights, *frequencies* is a 2-D :obj:`numpy.ndarray` of frequencies
        arranged [mode, qpt] and *eigenvectors* is a 4-D :obj:`numpy.ndarray`
        of complex phonon eigenvectors arranged [mode, qpt, atom, xyz].

    """
    logging.info("Reading phonon qpts, frequencies, weights, eigenvectors...")

    # Pre-allocate arrays
    qpts = np.zeros((header['nqpts'], 3))
    weights = np.zeros(header['nqpts'])
    frequencies = np.zeros((header['nbranches'], header['nqpts']))
    eigenvectors = np.zeros((header['nbranches'], header['nqpts'],
                             header['nions'], 3), dtype=np.complex64)

    # Scan past header
    with zopen(filename, 'rt') as f:
        for line in f:
            if line.strip() == 'END header':
                break
        else:
            raise IOError('Did not find "END header" line in phonon file "{}".'
                          ' File is not formatted correctly.'.format(filename))

        # Read data blocks
        for i_qpt in range(header['nqpts']):
            # Typical q-pt line format:
            # q-pt=   1   0.0000  0.0000  0.000   0.003012
            #
            #  -    INDEX ---------QPT---------    WEIGHT
            #  0      1      2      3       4         5

            qpt_line = f.readline().split()
            assert qpt_line[0] == 'q-pt='
            assert qpt_line[1] == str(i_qpt + 1)
            qpts[i_qpt, :] = list(map(float, qpt_line[2:5]))
            weights[i_qpt] = float(qpt_line[5])

            # Next come frequencies, typical line   "   1    3.25123"
            for i_mode in range(header['nbranches']):
                frequencies[i_mode, i_qpt] = float(f.readline().split()[1])

            # Next come eigenvectors, with some nice tabulation comments, e.g.
            #
            #                          Phonon Eigenvectors
            #   Mode Ion         X                Y               Z
            #      1   1  0.54308  0.0000  -0.7995  0.0000  0.2564  0.0000
            #      2   1 -0.60969  0.0000  -0.1654  0.0000  0.7751  0.0000
            assert f.readline().strip() == 'Phonon Eigenvectors'
            assert 'X' in f.readline().split()
            for i_mode in range(header['nbranches']):
                for i_ion in range(header['nions']):
                    eigenvector = np.fromstring(f.readline(), sep=' ')[2:]
                    eigenvector = eigenvector[::2] + 1j * eigenvector[1::2]
                    eigenvectors[i_mode, i_qpt, i_ion, :] = eigenvector

        return (qpts, weights, frequencies, eigenvectors)