from astropy.io import fits
import h5py
import os
import glob
from astropy.wcs import WCS
from astropy.wcs.utils import pixel_to_skycoord
import astropy.units as u
from tqdm import tqdm
import numpy as np
import keras
import pyregion
from scipy import interpolate
import time
from joblib import Parallel, delayed
from LUCI.LuciFit import Fit
import matplotlib.pyplot as plt
from astropy.nddata import Cutout2D
import astropy.stats as astrostats
from astroquery.astrometry_net import AstrometryNet
from astropy.io import fits
import multiprocessing
from astropy.time import Time
from astropy.coordinates import SkyCoord, EarthLocation
from numba import jit, set_num_threads
from LUCI.LuciNetwork import create_MDN_model, negative_loglikelihood
import numpy.ma as ma


class Luci():
    """
    This is the primary class for the general purpose line fitting code LUCI. This contains
    all io/administrative functionality. The fitting functionality can be found in the
    Fit class (Lucifit.py).
    """

    def __init__(self, Luci_path, cube_path, output_dir, object_name, redshift, resolution, ML_bool=True, mdn=False):
        """
        Initialize our Luci class -- this acts similar to the SpectralCube class
        of astropy or spectral-cube.

        Args:
            Luci_path: Path to Luci (must include trailing "/")
            cube_path: Full path to hdf5 cube with the hdf5 extension (e.x. '/user/home/M87.hdf5'; No trailing "/")
            output_dir: Full path to output directory
            object_name: Name of the object to fit. This is used for naming purposes. (e.x. 'M87')
            redshift: Redshift to the object. (e.x. 0.00428)
            resolution: Resolution requested of machine learning algorithm reference spectrum
            ML_bool: Boolean for applying machine learning; default=True
            mdn: Boolean for using the Mixed Density Network models; If true, then we use the posterior distributions calculated by our network as our priors for bayesian fits
        """
        self.Luci_path = Luci_path
        self.check_luci_path()  # Make sure the path is correctly written
        self.cube_path = cube_path
        self.output_dir = output_dir + '/Luci_outputs'
        if not os.path.exists(self.output_dir):
            os.mkdir(self.output_dir)
        self.object_name = object_name
        self.redshift = redshift
        self.resolution = resolution
        self.mdn = mdn
        self.quad_nb = 0  # Number of quadrants in Hdf5
        self.dimx = 0  # X dimension of cube
        self.dimy = 0  # Y dimension of cube
        self.dimz = 0  # Z dimension of cube
        self.cube_final = None  # Complete data cube
        self.cube_binned = None  # Binned data cube
        self.header = None
        self.deep_image = None
        self.spectrum_axis = None
        self.spectrum_axis_unshifted = None  # Spectrum axis without redshift change
        self.wavenumbers_syn = None
        self.wavenumbers_syn_full = None  # Unclipped reference spectrum
        self.hdr_dict = None
        self.interferometer_theta = None
        self.transmission_interpolated = None
        self.read_in_cube()
        self.step_nb = self.hdr_dict['STEPNB']
        self.zpd_index = self.hdr_dict['ZPDINDEX']
        self.filter = self.hdr_dict['FILTER']
        self.spectrum_axis_func()
        if ML_bool is True:
            if self.mdn != True:
                if self.filter in ['SN1', 'SN2', 'SN3', 'C4']:
                    self.ref_spec = self.Luci_path + 'ML/Reference-Spectrum-R%i-%s.fits' % (resolution, self.filter)
                    self.read_in_reference_spectrum()
                    self.model_ML = keras.models.load_model(
                        self.Luci_path + 'ML/R%i-PREDICTOR-I-%s' % (resolution, self.filter))
                else:
                    print(
                        'LUCI does not support machine learning parameter estimates for the filter you entered. Please set ML_bool=False.')
            else:  # mdn == True
                if self.filter in ['SN3']:
                    self.ref_spec = self.Luci_path + 'ML/Reference-Spectrum-R%i-%s.fits' % (resolution, self.filter)
                    self.read_in_reference_spectrum()
                    self.model_ML = create_MDN_model(len(self.wavenumbers_syn), negative_loglikelihood)
                    self.model_ML.load_weights(self.Luci_path + 'ML/R%i-PREDICTOR-I-MDN-%s/R%i-PREDICTOR-I-MDN-%s' % (
                    resolution, self.filter, resolution, self.filter))
                else:
                    print(
                        'LUCI does not support machine learning parameter estimates using a MDN for the filter you entered. Please set ML_bool=False or mdn=False.')
        else:
            self.model_ML = None
        self.read_in_transmission()

    def get_quadrant_dims(self, quad_number):
        """
        Calculate the x and y limits of a given quadrant in the HDF5 file. The
        data cube is saved in 9 individual arrays in the original HDF5 cube. This
        function gets the bouunds for each quadrant.

        Args:
            quad_number: Quadrant Number
        Return:
            x_min, x_max, y_min, y_max: Spatial bounds of quadrant
        """
        div_nb = int(np.sqrt(self.quad_nb))
        if (quad_number < 0) or (quad_number > self.quad_nb - 1):
            raise StandardError("quad_number out of bounds [0," + str(self.quad_nb - 1) + "]")
            return "SOMETHING FAILED"

        index_x = quad_number % div_nb
        index_y = (quad_number - index_x) / div_nb

        x_min = index_x * np.floor(self.dimx / div_nb)
        if (index_x != div_nb - 1):
            x_max = (index_x + 1) * np.floor(self.dimx / div_nb)
        else:
            x_max = self.dimx

        y_min = index_y * np.floor(self.dimy / div_nb)
        if (index_y != div_nb - 1):
            y_max = (index_y + 1) * np.floor(self.dimy / div_nb)
        else:
            y_max = self.dimy
        return int(x_min), int(x_max), int(y_min), int(y_max)

    def get_interferometer_angles(self, file):
        """
        Calculate the interferometer angle 2d array for the entire cube. We use
        the following equation:
        cos(theta) = lambda_ref/lambda
        where lambda_ref is the reference laser wavelength and lambda is the measured calibration laser wavelength.

        Args:
            file: hdf5 File object containing HDF5 file
        """
        calib_map = file['calib_map'][()]
        calib_ref = self.hdr_dict['CALIBNM']
        interferometer_cos_theta = calib_ref / calib_map  # .T[::-1,::-1]
        # We need to convert to degree so bear with me here
        self.interferometer_theta = np.rad2deg(np.arccos(interferometer_cos_theta))

    def update_header(self, file):
        """
        Create a standard WCS header from the HDF5 header. To do this we clean up the
        header data (which is initially stored in individual arrays). We then create
        a new header dictionary with the old cleaned header info. Finally, we use
        astropy.wcs.WCS to create an updated WCS header for the 2 spatial dimensions.
        This is then saved to self.header while the header dictionary is saved
        as self.hdr_dict.

        Args:
            file: hdf5 File object containing HDF5 file
        """
        hdr_dict = {}
        header_cols = [str(val[0]).replace("'b", '').replace("'", "").replace("b", '') for val in
                       list(file['header'][()])]
        header_vals = [str(val[1]).replace("'b", '').replace("'", "").replace("b", '') for val in
                       list(file['header'][()])]
        header_types = [val[3] for val in list(file['header'][()])]
        for header_col, header_val, header_type in zip(header_cols, header_vals, header_types):
            if 'bool' in str(header_type):
                hdr_dict[header_col] = bool(header_val)
            if 'float' in str(header_type):
                hdr_dict[header_col] = float(header_val)
            if 'int' in str(header_type):
                hdr_dict[header_col] = int(header_val)
            else:
                try:
                    hdr_dict[header_col] = float(header_val)
                except:
                    hdr_dict[header_col] = str(header_val)
        hdr_dict['CTYPE3'] = 'WAVE-SIP'
        hdr_dict['CUNIT3'] = 'm'
        # hdr_dict['NAXIS1'] = 2064
        # hdr_dict['NAXIS2'] = 2048
        # Make WCS
        wcs_data = WCS(hdr_dict, naxis=2)
        self.header = wcs_data.to_header()
        self.header.insert('WCSAXES', ('SIMPLE', 'T'))
        self.header.insert('SIMPLE', ('NAXIS', 2), after=True)
        # self.header.insert('NAXIS', ('NAXIS1', 2064), after=True)
        # self.header.insert('NAXIS1', ('NAXIS2', 2048), after=True)
        self.hdr_dict = hdr_dict

    def create_deep_image(self, output_name=None, binning=None):
        """
        Create deep image fits file of the cube. This takes the cube and sums
        the spectral axis. Then the deep image is saved as a fits file with the following
        naming convention: output_dir+'/'+object_name+'_deep.fits'. We also allow for
        the binning of the deep image -- this is used primarily for astrometry purposes.

        Args:
            output_name: Full path to output (optional)
            binning: Binning number (optional integer; default=None)
        """
        # hdu = fits.PrimaryHDU()
        # We are going to break up this calculation into chunks so that  we can have a progress bar
        # self.deep_image = np.sum(self.cube_final, axis=2).T

        hdf5_file = h5py.File(self.cube_path + '.hdf5', 'r')  # Open and read hdf5 file

        if 'deep_frame' in hdf5_file:  # A deep image already exists
            print('Existing deep frame extracted from hdf5 file.')
            self.deep_image = hdf5_file['deep_frame'][:]
            self.deep_image *= self.dimz
        else:  # Create new deep image
            print('New deep frame created from data.')
            self.deep_image = np.zeros(
                (self.cube_final.shape[0], self.cube_final.shape[1]))  # np.sum(self.cube_final, axis=2).T
            iterations_ = 10
            step_size = int(self.cube_final.shape[0] / iterations_)
            for i in tqdm(range(10)):
                self.deep_image[step_size * i:step_size * (i + 1)] = np.nansum(
                    self.cube_final[step_size * i:step_size * (i + 1)], axis=2)
        self.deep_image = self.deep_image.T
        # Bin data
        if binning != None and binning != 1:
            # Get cube size
            x_min = 0
            x_max = self.cube_final.shape[0]
            y_min = 0
            y_max = self.cube_final.shape[1]
            # Get new bin shape
            x_shape_new = int((x_max - x_min) / binning)
            y_shape_new = int((y_max - y_min) / binning)
            # Set to zero
            binned_deep = np.zeros((x_shape_new, y_shape_new))
            for i in range(x_shape_new):
                for j in range(y_shape_new):
                    # Bin
                    summed_deep = self.deep_image[x_min + int(i * binning):x_min + int((i + 1) * binning),
                                  y_min + int(j * binning):y_min + int((j + 1) * binning)]
                    summed_deep = np.nansum(summed_deep, axis=0)  # Sum along x
                    summed_deep = np.nansum(summed_deep, axis=0)  # Sum along y
                    binned_deep[i, j] = summed_deep  # Set to global
            # Update header information
            header_binned = self.header
            header_binned['CRPIX1'] = header_binned['CRPIX1'] / binning
            header_binned['CRPIX2'] = header_binned['CRPIX2'] / binning
            header_binned['CDELT1'] = header_binned['CDELT1'] * binning
            header_binned['CDELT2'] = header_binned['CDELT2'] * binning
            self.deep_image = binned_deep / (binning ** 2)
        if output_name == None:
            output_name = self.output_dir + '/' + self.object_name + '_deep.fits'
        fits.writeto(output_name, self.deep_image, self.header, overwrite=True)

    def read_in_cube(self):
        """
        Function to read the hdf5 data into a 3d numpy array (data cube). We also
        translate the header to standard wcs format by calling the update_header function.
        Note that the data are saved in several quadrants which is why we have to loop
        through them and place all the spectra in a single cube.
        """
        print('Reading in data...')
        file = h5py.File(self.cube_path + '.hdf5', 'r')  # Read in file
        self.quad_nb = file.attrs['quad_nb']  # Get the number of quadrants
        self.dimx = file.attrs['dimx']  # Get the dimensions in x
        self.dimy = file.attrs['dimy']  # Get the dimensions in y
        self.dimz = file.attrs['dimz']  # Get the dimensions in z (spectral axis)
        self.cube_final = np.zeros((self.dimx, self.dimy, self.dimz))  # Complete data cube
        for iquad in tqdm(range(self.quad_nb)):
            xmin, xmax, ymin, ymax = self.get_quadrant_dims(iquad)
            iquad_data = file['quad00%i' % iquad]['data'][:]  # Save data to intermediate array
            iquad_data[(np.isfinite(iquad_data) == False)] = 1e-22  # Set infinite values to 1-e22
            iquad_data[(iquad_data < -1e-16)] = 1e-22  # Set high negative flux values to 1e-22
            iquad_data[(iquad_data > 1e-9)] = 1e-22  # Set unrealistically high positive flux values to 1e-22
            self.cube_final[xmin:xmax, ymin:ymax, :] = iquad_data  # Save to correct location in main cube
        self.cube_final = self.cube_final  # .transpose(1, 0, 2)
        self.update_header(file)
        self.get_interferometer_angles(file)

    def spectrum_axis_func(self):
        """
        Create the x-axis for the spectra. We must construct this from header information
        since each pixel only has amplitudes of the spectra at each point.
        """
        len_wl = self.hdr_dict['STEPNB']  # Length of Spectral Axis
        start = self.hdr_dict['CRVAL3']  # Starting value of the spectral x-axis
        end = start + (len_wl) * self.hdr_dict['CDELT3']  # End
        step = self.hdr_dict['CDELT3']  # Step size
        self.spectrum_axis = np.array(np.linspace(start, end, len_wl) * (self.redshift + 1),
                                      dtype=np.float32)  # Apply redshift correction
        self.spectrum_axis_unshifted = np.array(np.linspace(start, end, len_wl),
                                                dtype=np.float32)  # Do not apply redshift correction

        # min_ = 1e7  * (self.hdr_dict['ORDER'] / (2*self.hdr_dict['STEP']))# + 1e7  / (2*self.delta_x*self.n_steps)
        # max_ = 1e7  * ((self.hdr_dict['ORDER'] + 1) / (2*self.hdr_dict['STEP']))# - 1e7  / (2*self.delta_x*self.n_steps)
        # step_ = max_ - min_
        # axis = np.array([min_+j*step_/self.hdr_dict['STEPNB'] for j in range(self.hdr_dict['STEPNB'])])
        # self.spectrum_axis = axis#*(1+self.redshift)
        # self.spectrum_axis_unshifted = axis

    def read_in_reference_spectrum(self):
        """
        Read in the reference spectrum that will be used in the machine learning
        algorithm to interpolate the true spectra so that they
        wil all have the same size (required for our CNN). The reference spectrum
        will be saved as self.wavenumbers_syn [cm-1].
        """
        ref_spec = fits.open(self.ref_spec)[1].data
        channel = []
        counts = []
        for chan in ref_spec:  # Only want SN3 region
            channel.append(chan[0])
            counts.append(np.real(chan[1]))
        if self.hdr_dict['FILTER'] == 'SN3':
            min_ = np.argmin(np.abs(np.array(channel) - 14700))
            max_ = np.argmin(np.abs(np.array(channel) - 15600))
        elif self.hdr_dict['FILTER'] == 'SN2':
            min_ = np.argmin(np.abs(np.array(channel) - 19000))
            max_ = np.argmin(np.abs(np.array(channel) - 21000))
        elif self.hdr_dict['FILTER'] == 'SN1':
            min_ = np.argmin(np.abs(np.array(channel) - 25500))
            max_ = np.argmin(np.abs(np.array(channel) - 27500))
        elif self.hdr_dict['FILTER'] == 'C4':
            min_ = np.argmin(np.abs(np.array(channel) - 14700))
            max_ = np.argmin(np.abs(np.array(channel) - 15600))
        else:
            print('We do not support this filter.')
            print('Terminating program!')
            exit()
        self.wavenumbers_syn = np.array(channel[min_:max_], dtype=np.float32)
        self.wavenumbers_syn_full = np.array(channel, dtype=np.float32)

    def read_in_transmission(self):
        """
        Read in the transmission spectrum for the filter. Then apply interpolation
        on it to make it have the same x-axis as the spectra.
        """
        transmission = np.loadtxt('%s/Data/%s_filter.dat' % (
        self.Luci_path, self.hdr_dict['FILTER']))  # first column - axis; second column - value
        f = interpolate.interp1d(transmission[:, 0], [val / 100 for val in transmission[:, 1]], kind='slinear',
                                 fill_value="extrapolate")
        self.transmission_interpolated = f(self.spectrum_axis_unshifted)

    def bin_cube(self, binning, x_min, x_max, y_min, y_max):
        """
        Function to bin cube into bin x bin sub cubes

        Args:
            binning: Size of binning (equal in x and y direction)
            x_min: Lower bound in x
            x_max: Upper bound in x
            y_min: Lower bound in y
            y_max: Upper bound in y
        Return:
            Binned cubed called self.cube_binned and new spatial limits
        """
        x_shape_new = int((x_max - x_min) / binning)
        y_shape_new = int((y_max - y_min) / binning)
        binned_cube = np.zeros((x_shape_new, y_shape_new, self.cube_final.shape[2]))
        for i in range(x_shape_new):
            for j in range(y_shape_new):
                summed_spec = self.cube_final[x_min + int(i * binning):x_min + int((i + 1) * binning),
                              y_min + int(j * binning):y_min + int((j + 1) * binning), :]
                summed_spec = np.nansum(summed_spec, axis=0)
                summed_spec = np.nansum(summed_spec, axis=0)
                binned_cube[i, j] = summed_spec[:]
        self.header_binned = self.header
        self.header_binned['CRPIX1'] = self.header_binned['CRPIX1'] / binning
        self.header_binned['CRPIX2'] = self.header_binned['CRPIX2'] / binning
        self.header_binned['CDELT1'] = self.header_binned['CDELT1'] * binning
        self.header_binned['CDELT2'] = self.header_binned['CDELT2'] * binning
        self.cube_binned = binned_cube / (binning ** 2)

    def bin_mask(self, mask, binning, x_min, x_max, y_min, y_max):
        """
        Function to bin mask. This is effectively the same as `self.bin_cube` with
        the exception that it is for the mask only. For now, this function is only
        triggered when the mask is in the form of a '.npy' file. Region files
        passed as '.reg' files do not need to be additionally masked since they use
        the binned header information.

        Args:
            mask: Mask to be binned
            binning: Size of binning (equal in x and y direction)
            x_min: Lower bound in x
            x_max: Upper bound in x
            y_min: Lower bound in y
            y_max: Upper bound in y
        Return:
            Binned cubed called self.cube_binned and new spatial limits
        """
        x_shape_new = int((x_max - x_min) / binning)
        y_shape_new = int((y_max - y_min) / binning)
        binned_mask = np.zeros((x_shape_new, y_shape_new))
        for i in range(x_shape_new):
            for j in range(y_shape_new):
                summed_spec = mask[x_min + int(i * binning):x_min + int((i + 1) * binning),
                              y_min + int(j * binning):y_min + int((j + 1) * binning)]
                summed_spec = np.nansum(summed_spec, axis=0)
                summed_spec = np.nansum(summed_spec, axis=0)
                binned_mask[i, j] = summed_spec[:]
        self.binned_mask = binned_mask / (binning ** 2)

    def save_fits(self, lines, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, broadenings_fits,
                  velocities_errors_fits, broadenings_errors_fits, chi2_fits, continuum_fits, header, binning):
        """
        Function to save the fits files returned from the fitting routine. We save the velocity, broadening,
        amplitude, flux, and chi-squared maps with the appropriate headers in the output directory
        defined when the cube is initiated.

        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            ampls_fits: 3D Numpy array of amplitude values
            flux_fis: 3D Numpy array of flux values
            flux_errors_fits 3D numpy array of flux errors
            velocities_fits: 3D Numpy array of velocity values
            broadenings_fits: 3D Numpy array of broadening values
            velocities_errors_fits: 3D Numpy array of velocity errors
            broadenings_errors_fits: 3D Numpy array of broadening errors
            chi2_fits: 2D Numpy array of chi-squared values
            continuum_fits: 2D Numpy array of continuum value
            header: Header object (either binned or unbinned)
            output_name: Output directory and naming convention
            binning: Value by which to bin (default None)

        """
        # Make sure output dirs exist for amps, flux, vel, and broad
        if not os.path.exists(self.output_dir + '/Amplitudes'):
            os.mkdir(self.output_dir + '/Amplitudes')
        if not os.path.exists(self.output_dir + '/Fluxes'):
            os.mkdir(self.output_dir + '/Fluxes')
        if not os.path.exists(self.output_dir + '/Velocity'):
            os.mkdir(self.output_dir + '/Velocity')
        if not os.path.exists(self.output_dir + '/Broadening'):
            os.mkdir(self.output_dir + '/Broadening')

        if binning is not None:
            output_name = self.object_name + "_" + str(binning)
        else:
            output_name = self.object_name
        lines_fit = []  # List of lines which already have maps
        for ct, line_ in enumerate(lines):  # Step through each line to save their individual amplitudes
            if lines_fit.count(line_) >= 1:  # If the line is already present in the list of lines create
                # This means multiple components were fit of this line so they need to be nammed appropriately
                line_number = lines_fit.count(line_) + 1
                line_ += '_' + str(line_number)
            fits.writeto(self.output_dir + '/Amplitudes/' + output_name + '_' + line_ + '_Amplitude.fits',
                         ampls_fits[:, :, ct], header, overwrite=True)
            fits.writeto(self.output_dir + '/Fluxes/' + output_name + '_' + line_ + '_Flux.fits', flux_fits[:, :, ct],
                         header, overwrite=True)
            fits.writeto(self.output_dir + '/Fluxes/' + output_name + '_' + line_ + '_Flux_err.fits',
                         flux_errors_fits[:, :, ct], header, overwrite=True)
            fits.writeto(self.output_dir + '/Velocity/' + output_name + '_' + line_ + '_velocity.fits',
                         velocities_fits[:, :, ct], header, overwrite=True)
            fits.writeto(self.output_dir + '/Broadening/' + output_name + '_' + line_ + '_broadening.fits',
                         broadenings_fits[:, :, ct], header, overwrite=True)
            fits.writeto(self.output_dir + '/Velocity/' + output_name + '_' + line_ + '_velocity_err.fits',
                         velocities_errors_fits[:, :, ct], header, overwrite=True)
            fits.writeto(self.output_dir + '/Broadening/' + output_name + '_' + line_ + '_broadening_err.fits',
                         broadenings_errors_fits[:, :, ct], header, overwrite=True)
        fits.writeto(output_name + '_Chi2.fits', chi2_fits, header, overwrite=True)
        fits.writeto(output_name + '_continuum.fits', continuum_fits, header, overwrite=True)

    def fit_entire_cube(self, lines, fit_function, vel_rel, sigma_rel, bkg=None, binning=None, bayes_bool=False,
                        output_name=None, uncertainty_bool=False, n_threads=1):
        """
        Fit the entire cube (all spatial dimensions)
        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            fit_function: Fitting function to use (e.x. 'gaussian')
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            bkg: Background Spectrum (1D numpy array; default None)
            binning:  Value by which to bin (default None)
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis (default False)
            output_name: User defined output path/name (default None)
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            n_threads: Number of threads to be passed to joblib for parallelization (default = 1)

        Return:
            Velocity and Broadening arrays (2d). Also return amplitudes array (3D).
        """
        x_min = 0
        x_max = self.cube_final.shape[0]
        y_min = 0
        y_max = self.cube_final.shape[1]
        self.fit_cube(lines, fit_function, vel_rel, sigma_rel, x_min, x_max, y_min, y_max)

    def fit_cube(self, lines, fit_function, vel_rel, sigma_rel,
                 x_min, x_max, y_min, y_max, bkg=None, binning=None,
                 bayes_bool=False, bayes_method='emcee',
                 uncertainty_bool=False, n_threads=1, nii_cons=True, initial_values=False):
        """
        Primary fit call to fit rectangular regions in the data cube. This wraps the
        LuciFits.FIT().fit() call which applies all the fitting steps. This also
        saves the velocity and broadening fits files. All the files will be saved
        in the folder Luci. The files are the fluxes, velocities, broadening, amplitudes,
        and continuum (and their associated errors) for each line.

        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            fit_function: Fitting function to use (e.x. 'gaussian')
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            x_min: Lower bound in x
            x_max: Upper bound in x
            y_min: Lower bound in y
            y_max: Upper bound in y
            bkg: Background Spectrum (1D numpy array; default None)
            binning:  Value by which to bin (default None)
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis (default False)
            bayes_method = Bayesian Inference method. Options are '[emcee', 'dynesty'] (default 'emcee')
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            n_threads: Number of threads to be passed to joblib for parallelization (default = 1)
            nii_cons: Boolean to turn on or off NII doublet ratio constraint (default True)
            initial_values: List of files containing initial conditions (default False)
        Return:
            Velocity and Broadening arrays (2d). Also return amplitudes array (3D).

        Examples:
            As always, we must first have the cube initialized (see basic example).

            If we want to fit all five lines in SN3 with a sincgauss function and binning of 2
            over a rectangular region defined in image coordinates as 800<x<1500; 250<y<1250,
            we would run the following:

            >>> vel_map, broad_map, flux_map, chi2_fits = cube.fit_cube(['Halpha', 'NII6548', 'NII6583', 'SII6716', 'SII6731'], 'sincgauss', [1,1,1,1,1], [1,1,1,1,1], 800, 1500, 250, 750, binning=2)

        """
        # Initialize fit solution arrays
        if binning != None and binning != 1:
            self.bin_cube(binning, x_min, x_max, y_min, y_max)
            x_max = int((x_max - x_min) / binning);
            y_max = int((y_max - y_min) / binning)
            x_min = 0;
            y_min = 0
        elif binning == 1:
            pass  # Don't do anything if binning is set to 1
        chi2_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        corr_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        step_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        # First two dimensions are the X and Y dimensions.
        # The third dimension corresponds to the line in the order of the lines input parameter.
        ampls_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        flux_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        flux_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        velocities_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        broadenings_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        velocities_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0,
                                                                                                                  2)
        broadenings_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0,
                                                                                                                   2)
        continuum_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        set_num_threads(n_threads)
        # Initialize initiatl conditions for velocity and broadening as False --> Assuming we don't have them
        vel_init = False
        broad_init = False
        # TODO: READ IN INITIAL CONDITIONS
        if initial_values is not False:
            # Obtain initial condition maps from files
            vel_init = fits.open(initial_values[0])[0].data
            broad_init = fits.open(initial_values[0])[0].data

        @jit(nopython=False)
        def fit_calc(i, ampls_fit, flux_fit, flux_errs_fit, vels_fit, vels_errs_fit, broads_fit, broads_errs_fit,
                     chi2_fit, corr_fit, step_fit, continuum_fit):
            y_pix = y_min + i  # Step y coordinate
            # Set up all the local lists for the current y_pixel step
            ampls_local = []
            flux_local = []
            flux_errs_local = []
            vels_local = []
            broads_local = []
            vels_errs_local = []
            broads_errs_local = []
            chi2_local = []
            corr_local = []
            step_local = []
            continuum_local = []
            # Step through x coordinates
            for j in range(x_max - x_min):
                x_pix = x_min + j  # Set current x pixel
                if binning is not None and binning != 1:  # If binning, then take spectrum from binned cube
                    sky = self.cube_binned[x_pix, y_pix, :]
                else:  # If not, then take from the unbinned cube
                    sky = self.cube_final[x_pix, y_pix, :]
                if bkg is not None:  # If there is a background variable subtract the bkg spectrum
                    if binning:  # If binning, then we have to take into account how many pixels are in each bin
                        sky -= bkg * binning ** 2  # Subtract background spectrum
                    else:  # No binning so just subtract the background directly
                        sky -= bkg  # Subtract background spectrum
                good_sky_inds = [~np.isnan(sky)]  #  Find all NaNs in sky spectrum
                sky = sky[good_sky_inds]  # Clean up spectrum by dropping any Nan values
                axis = self.spectrum_axis[good_sky_inds]  # Clean up axis  accordingly
                # TODO: PASS INITIAL CONDITIONS
                if vel_init is not False and broad_init is not False:  # If initial conditions were passed
                    initial_conditions = [vel_init[x_pix, y_pix], broad_init[x_pix, y_pix]]
                # Call fit!
                if len(sky) > 0:  # Ensure that there are values in sky
                    fit = Fit(sky, axis, self.wavenumbers_syn, fit_function, lines, vel_rel, sigma_rel,
                              self.model_ML, trans_filter=self.transmission_interpolated,
                              theta=self.interferometer_theta[x_pix, y_pix],
                              delta_x=self.hdr_dict['STEP'], n_steps=self.step_nb,
                              zpd_index=self.zpd_index,
                              filter=self.hdr_dict['FILTER'],
                              bayes_bool=bayes_bool, bayes_method=bayes_method,
                              uncertainty_bool=uncertainty_bool,
                              mdn=self.mdn, nii_cons=nii_cons, initial_values=initial_values
                              )
                    fit_dict = fit.fit()  # Collect fit dictionary
                    # Save local list of fit values
                    ampls_local.append(fit_dict['amplitudes'])
                    flux_local.append(fit_dict['fluxes'])
                    flux_errs_local.append(fit_dict['flux_errors'])
                    vels_local.append(fit_dict['velocities'])
                    broads_local.append(fit_dict['sigmas'])
                    vels_errs_local.append(fit_dict['vels_errors'])
                    broads_errs_local.append(fit_dict['sigmas_errors'])
                    chi2_local.append(fit_dict['chi2'])
                    corr_local.append(fit_dict['corr'])
                    step_local.append(fit_dict['axis_step'])
                    continuum_local.append(fit_dict['continuum'])
                else:  # If the sky is empty (this rarely rarely rarely happens), then return zeros for everything
                    ampls_local.append([0] * len(lines))
                    flux_local.append([0] * len(lines))
                    flux_errs_local.append([0] * len(lines))
                    vels_local.append([0] * len(lines))
                    broads_local.append([0] * len(lines))
                    vels_errs_local.append([0] * len(lines))
                    broads_errs_local.append([0] * len(lines))
                    chi2_local.append(0)
                    corr_local.append(0)
                    step_local.append(0)
                    continuum_local.append(0)
            ampls_fits[i] = ampls_local
            flux_fits[i] = flux_local
            flux_errors_fits[i] = flux_errs_local
            velocities_fits[i] = vels_local
            broadenings_fits[i] = broads_local
            velocities_errors_fits[i] = vels_errs_local
            broadenings_errors_fits[i] = broads_errs_local
            chi2_fits[i] = chi2_local
            corr_fits[i] = corr_local
            step_fits[i] = step_local
            continuum_fits[i] = continuum_local
            return i, ampls_fit, flux_fit, flux_errs_fit, vels_fit, vels_errs_fit, broads_fit, broads_errs_fit, chi2_fit, corr_fit, step_fit, continuum_fit

        # Write outputs (Velocity, Broadening, and Amplitudes)
        if binning is not None and binning != 1:
            # Check if deep image exists: if not, create it
            if not os.path.exists(self.output_dir + '/' + self.object_name + '_deep.fits'):
                self.create_deep_image()
            wcs = WCS(self.header_binned)
        else:
            # Check if deep image exists: if not, create it
            if not os.path.exists(self.output_dir + '/' + self.object_name + '_deep.fits'):
                self.create_deep_image()
            wcs = WCS(self.header, naxis=2)
        cutout = Cutout2D(fits.open(self.output_dir + '/' + self.object_name + '_deep.fits')[0].data,
                          position=((x_max + x_min) / 2, (y_max + y_min) / 2), size=(x_max - x_min, y_max - y_min),
                          wcs=wcs)
        for step_i in tqdm(range(y_max - y_min)):
            fit_calc(step_i, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, velocities_errors_fits,
                     broadenings_fits, broadenings_errors_fits, chi2_fits, corr_fits, step_fits, continuum_fits)
        self.save_fits(lines, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, broadenings_fits,
                       velocities_errors_fits, broadenings_errors_fits, chi2_fits, continuum_fits,
                       cutout.wcs.to_header(), binning)

        return velocities_fits, broadenings_fits, flux_fits, chi2_fits

    def fit_region(self, lines, fit_function, vel_rel, sigma_rel, region,
                   bkg=None, binning=None, bayes_bool=False, bayes_method='emcee',
                   output_name=None, uncertainty_bool=False, n_threads=1, nii_cons=True):
        """
        Fit the spectrum in a region. This is an extremely similar command to fit_cube except
        it works for ds9 regions. We first create a mask from the ds9 region file. Then
        we step through the cube and only fit the unmasked pixels. Although this may not
        be the most efficient method, it does ensure the fidelity of the wcs system.
        All the files will be saved
        in the folder Luci. The files are the fluxes, velocities, broadening, amplitudes,
        and continuum (and their associated errors) for each line.

        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            fit_function: Fitting function to use (e.x. 'gaussian')
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2])
            region: Name of ds9 region file (e.x. 'region.reg'). You can also pass a boolean mask array.
            bkg: Background Spectrum (1D numpy array; default None)
            binning:  Value by which to bin (default None)
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis (default False)
            bayes_method: Bayesian Inference method. Options are '[emcee', 'dynesty'] (default 'emcee')
            output_name: User defined output path/name
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            n_threads: Number of threads to be passed to joblib for parallelization (default = 1)
            nii_cons: Boolean to turn on or off NII doublet ratio constraint (default True)
        Return:
            Velocity and Broadening arrays (2d). Also return amplitudes array (3D).

        Examples:
            As always, we must first have the cube initialized (see basic example).

            If we want to fit all five lines in SN3 with a gaussian function and no binning
            over a ds9 region called main.reg, we would run the following:

            >>> vel_map, broad_map, flux_map, chi2_fits = cube.fit_region(['Halpha', 'NII6548', 'NII6583', 'SII6716', 'SII6731'], 'gaussian', [1,1,1,1,1], [1,1,1,1,1],region='main.reg')

            We could also enable uncertainty calculations and parallel fitting:

            >>> vel_map, broad_map, flux_map, chi2_fits = cube.fit_region(['Halpha', 'NII6548', 'NII6583', 'SII6716', 'SII6731'], 'gaussian', [1,1,1,1,1], [1,1,1,1,1], region='main.reg', uncertatinty_bool=True, n_threads=4)

        """
        # Set spatial bounds for entire cube
        x_min = 0
        x_max = self.cube_final.shape[0]
        y_min = 0
        y_max = self.cube_final.shape[1]
        # Initialize fit solution arrays
        if binning != None and binning != 1:
            self.bin_cube(binning, x_min, x_max, y_min, y_max)
            # x_min = int(x_min/binning) ; y_min = int(y_min/binning) ; x_max = int(x_max/binning) ;  y_max = int(y_max/binning)
            x_max = int((x_max - x_min) / binning);
            y_max = int((y_max - y_min) / binning)
            x_min = 0;
            y_min = 0
        # Create mask
        if '.reg' in region:
            shape = (2064, 2048)  # (self.header["NAXIS1"], self.header["NAXIS2"])  # Get the shape
            if binning != None and binning > 1:
                header = self.header_binned
            else:
                header = self.header
            header.set('NAXIS1', 2064)
            header.set('NAXIS2', 2048)
            r = pyregion.open(region).as_imagecoord(header)  # Obtain pyregion region
            mask = r.get_mask(shape=shape).T  # Calculate mask from pyregion region
        elif '.npy' in region:
            mask = np.load(region)
        else:
            print("At the moment, we only support '.reg' and '.npy' files for masks.")
            print("Terminating Program!")
        # Clean up output name
        if isinstance(region, str):
            if len(region.split('/')) > 1:  # If region file is a path, just keep the name for output purposes
                region = region.split('/')[-1]
            if output_name is None:
                output_name = self.output_dir + '/' + self.object_name + '_' + region.split('.')[0]
        else:  # Passed mask not region file
            if output_name is None:
                output_name = self.output_dir + '/' + self.object_name + '_mask'

        chi2_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        # First two dimensions are the X and Y dimensions.
        # The third dimension corresponds to the line in the order of the lines input parameter.
        ampls_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        flux_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        flux_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        velocities_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        broadenings_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0, 2)
        velocities_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0,
                                                                                                                  2)
        broadenings_errors_fits = np.zeros((x_max - x_min, y_max - y_min, len(lines)), dtype=np.float32).transpose(1, 0,
                                                                                                                   2)
        continuum_fits = np.zeros((x_max - x_min, y_max - y_min), dtype=np.float32).T
        ct = 0
        set_num_threads(n_threads)

        @jit(nopython=False)
        def fit_calc(i, ampls_fit, flux_fit, flux_errs_fit, vels_fit, vels_errs_fit, broads_fit, broads_errs_fit,
                     chi2_fit, continuum_fit):
            # for i in tqdm(range(y_max-y_min)):
            y_pix = y_min + i
            ampls_local = []
            flux_local = []
            flux_errs_local = []
            vels_local = []
            broads_local = []
            vels_errs_local = []
            broads_errs_local = []
            chi2_local = []
            continuum_local = []
            for j in range(x_max - x_min):
                x_pix = x_min + j
                # Check if pixel is in the mask or not
                # If so, fit as normal. Else, set values to zero
                if mask[x_pix, y_pix] == True:
                    if binning is not None and binning != 1:
                        sky = self.cube_binned[x_pix, y_pix, :]
                    else:
                        sky = self.cube_final[x_pix, y_pix, :]
                    if bkg is not None:
                        if binning:
                            sky -= bkg * binning ** 2  # Subtract background spectrum
                        else:
                            sky -= bkg  # Subtract background spectrum
                    good_sky_inds = [~np.isnan(sky)]  # Clean up spectrum
                    sky = sky[good_sky_inds]
                    axis = self.spectrum_axis[good_sky_inds]
                    # Call fit!
                    fit = Fit(sky, axis, self.wavenumbers_syn, fit_function, lines, vel_rel, sigma_rel,
                              self.model_ML, trans_filter=self.transmission_interpolated,
                              theta=self.interferometer_theta[x_pix, y_pix],
                              delta_x=self.hdr_dict['STEP'], n_steps=self.step_nb,
                              zpd_index=self.zpd_index,
                              filter=self.hdr_dict['FILTER'],
                              bayes_bool=bayes_bool, bayes_method=bayes_method,
                              uncertainty_bool=uncertainty_bool,
                              mdn=self.mdn, nii_cons=nii_cons)
                    fit_dict = fit.fit()
                    # Save local list of fit values
                    ampls_local.append(fit_dict['amplitudes'])
                    flux_local.append(fit_dict['fluxes'])
                    flux_errs_local.append(fit_dict['flux_errors'])
                    vels_local.append(fit_dict['velocities'])
                    broads_local.append(fit_dict['sigmas'])
                    vels_errs_local.append(fit_dict['vels_errors'])
                    broads_errs_local.append(fit_dict['sigmas_errors'])
                    chi2_local.append(fit_dict['chi2'])
                    continuum_local.append(fit_dict['continuum'])
                else:  # If outside of mask set to zero
                    ampls_local.append([0] * len(lines))
                    flux_local.append([0] * len(lines))
                    flux_errs_local.append([0] * len(lines))
                    vels_local.append([0] * len(lines))
                    broads_local.append([0] * len(lines))
                    vels_errs_local.append([0] * len(lines))
                    broads_errs_local.append([0] * len(lines))
                    chi2_local.append(0)
                    continuum_local.append(0)
            ampls_fits[i] = ampls_local
            flux_fits[i] = flux_local
            flux_errors_fits[i] = flux_errs_local
            velocities_fits[i] = vels_local
            broadenings_fits[i] = broads_local
            velocities_errors_fits[i] = vels_errs_local
            broadenings_errors_fits[i] = broads_errs_local
            chi2_fits[i] = chi2_local
            continuum_fits[i] = continuum_local
            return i, ampls_fit, flux_fit, flux_errs_fit, vels_fit, vels_errs_fit, broads_fit, broads_errs_fit, chi2_fit, continuum_fit

        # Write outputs (Velocity, Broadening, and Amplitudes)
        if binning is not None and binning > 1:
            # Check if deep image exists: if not, create it
            if not os.path.exists(self.output_dir + '/' + self.object_name + '_deep.fits'):
                self.create_deep_image()
            wcs = WCS(self.header_binned)
        else:
            # Check if deep image exists: if not, create it
            if not os.path.exists(self.output_dir + '/' + self.object_name + '_deep.fits'):
                self.create_deep_image()
            wcs = WCS(self.header, naxis=2)
        cutout = Cutout2D(fits.open(self.output_dir + '/' + self.object_name + '_deep.fits')[0].data,
                          position=((x_max + x_min) / 2, (y_max + y_min) / 2), size=(x_max - x_min, y_max - y_min),
                          wcs=wcs)
        for step_i in tqdm(range(y_max - y_min)):
            fit_calc(step_i, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, velocities_errors_fits,
                     broadenings_fits, broadenings_errors_fits, chi2_fits, continuum_fits)
        self.save_fits(lines, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, broadenings_fits,
                       velocities_errors_fits, broadenings_errors_fits, chi2_fits, continuum_fits,
                       cutout.wcs.to_header(), binning)
        return velocities_fits, broadenings_fits, flux_fits, chi2_fits, mask

    def fit_pixel(self, lines, fit_function, vel_rel, sigma_rel,
                  pixel_x, pixel_y, bin=None, bkg=None,
                  bayes_bool=False, bayes_method='emcee',
                  uncertainty_bool=False,
                  nii_cons=True):
        """
        Primary fit call to fit a single pixel in the data cube. This wraps the
        LuciFits.FIT().fit() call which applies all the fitting steps.

        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            fit_function: Fitting function to use (e.x. 'gaussian')
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            pixel_x: X coordinate (physical)
            pixel_y: Y coordinate (physical)
            bin: Number of pixels to take around coordinate (i.e. bin=1 will take all pixels touching the X and Y coordinates.
            bkg: Background Spectrum (1D numpy array; default None)
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis (default False)
            bayes_method: Bayesian Inference method. Options are '[emcee', 'dynesty'] (default 'emcee')
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            nii_cons: Boolean to turn on or off NII doublet ratio constraint (default True)
        Return:
            Returns the x-axis (redshifted), sky, and fit dictionary


        """
        sky = None
        if bin is not None and bin != 1:  # If data is binned
            sky = self.cube_final[pixel_x-bin:pixel_x+bin, pixel_y-bin:pixel_y+bin, :]
            sky = np.nansum(sky, axis=0)
            sky = np.nansum(sky, axis=0)
            if bkg is not None:
                sky -= bkg * (2 + bin)**2  # Subtract background times number of pixels
        else:
            sky = self.cube_final[pixel_x, pixel_y, :]
            if bkg is not None:
                sky -= bkg  # Subtract background spectrum
        good_sky_inds = [~np.isnan(sky)]  # Clean up spectrum
        sky = sky[good_sky_inds]  # Apply clean to sky
        axis = self.spectrum_axis[good_sky_inds]  # Apply clean to axis
        # Call fit!
        fit = Fit(sky, axis, self.wavenumbers_syn, fit_function, lines, vel_rel, sigma_rel,
                  self.model_ML, trans_filter=self.transmission_interpolated,
                  theta=self.interferometer_theta[pixel_x, pixel_y],
                  delta_x=self.hdr_dict['STEP'], n_steps=self.step_nb,
                  zpd_index=self.zpd_index,
                  filter=self.hdr_dict['FILTER'],
                  bayes_bool=bayes_bool, bayes_method=bayes_method,
                  uncertainty_bool=uncertainty_bool,
                  mdn=self.mdn, nii_cons=nii_cons)
        fit_dict = fit.fit()
        return axis, sky, fit_dict

    def extract_spectrum(self, x_min, x_max, y_min, y_max, bkg=None, binning=None, mean=False):
        """
        Extract spectrum in region. This is primarily used to extract background regions.
        The spectra in the region are summed and then averaged (if mean is selected).
        Using the 'mean' argument, we can either calculate the total summed spectrum (False)
        or the averaged spectrum for background spectra (True).

        Args:
            x_min: Lower bound in x
            x_max: Upper bound in x
            y_min: Lower bound in y
            y_max: Upper bound in y
            bkg: Background Spectrum (1D numpy array; default None)
            binning:  Value by which to bin (default None)
            mean: Boolean to determine whether or not the mean spectrum is taken. This is used for calculating background spectra.
        Return:
            X-axis (redshifted) and spectral axis of region.

        """
        integrated_spectrum = np.zeros(self.cube_final.shape[2])
        spec_ct = 0
        axis = None  # Initialize
        # Initialize fit solution arrays
        if binning != None and binning != 1:
            self.bin_cube(binning, x_min, x_max, y_min, y_max)
            # x_min = int(x_min/binning) ; y_min = int(y_min/binning) ; x_max = int(x_max/binning) ;  y_max = int(y_max/binning)
            x_max = int((x_max - x_min) / binning);
            y_max = int((y_max - y_min) / binning)
            x_min = 0;
            y_min = 0
        for i in tqdm(range(y_max - y_min)):
            y_pix = y_min + i
            for j in range(x_max - x_min):
                x_pix = x_min + j
                if binning is not None and binning != 1:
                    sky = self.cube_binned[x_pix, y_pix, :]
                else:
                    sky = self.cube_final[x_pix, y_pix, :]
                if bkg is not None:
                    if binning:
                        sky -= bkg * binning ** 2  # Subtract background spectrum
                    else:
                        sky -= bkg  # Subtract background spectrum
                good_sky_inds = [~np.isnan(sky)]  # Clean up spectrum
                integrated_spectrum += sky[good_sky_inds]
                if spec_ct == 0:
                    axis = self.spectrum_axis[good_sky_inds]
                    spec_ct += 1
        if mean:
            integrated_spectrum /= spec_ct
        return axis, integrated_spectrum

    def extract_spectrum_region(self, region, mean=False):
        """
        Extract spectrum in region. This is primarily used to extract background regions.
        The spectra in the region are summed and then averaged (if mean is selected).
        Using the 'mean' argument, we can either calculate the total summed spectrum (False)
        or the averaged spectrum for background spectra (True).

        Args:
            region: Name of ds9 region file (e.x. 'region.reg'). You can also pass a boolean mask array.
            mean: Boolean to determine whether or not the mean spectrum is taken. This is used for calculating background spectra.
        Return:
            X-axis and spectral axis of region.

        """
        # Create mask
        if '.reg' in region:
            shape = (2064, 2048)  # (self.header["NAXIS1"], self.header["NAXIS2"])  # Get the shape
            r = pyregion.open(region).as_imagecoord(self.header)  # Obtain pyregion region
            mask = r.get_mask(shape=shape).T  # Calculate mask from pyregion region
        elif '.npy' in region:
            mask = np.load(region)
        else:
            print("At the moment, we only support '.reg' and '.npy' files for masks.")
            print("Terminating Program!")

        # Set spatial bounds for entire cube
        x_min = 0
        x_max = self.cube_final.shape[0]
        y_min = 0
        y_max = self.cube_final.shape[1]
        integrated_spectrum = np.zeros(self.cube_final.shape[2])
        spec_ct = 0
        for i in tqdm(range(y_max - y_min)):
            y_pix = y_min + i
            for j in range(x_max - x_min):
                x_pix = x_min + j
                # Check if pixel is in the mask or not
                if mask[x_pix, y_pix]:
                    integrated_spectrum += self.cube_final[x_pix, y_pix, :]
                    spec_ct += 1
                else:
                    pass
        if mean:
            integrated_spectrum /= spec_ct
        return self.spectrum_axis, integrated_spectrum

    def fit_spectrum_region(self, lines, fit_function, vel_rel, sigma_rel,
                            region, bkg=None,
                            bayes_bool=False, bayes_method='emcee',
                            uncertainty_bool=False, mean=False, nii_cons=True
                            ):
        """
        Fit spectrum in region.
        The spectra in the region are summed and then averaged (if mean is selected).
        Using the 'mean' argument, we can either calculate the total summed spectrum (False)
        or the averaged spectrum for background spectra (True).

        Args:
            lines: Lines to fit (e.x. ['Halpha', 'NII6583'])
            fit_function: Fitting function to use (e.x. 'gaussian')
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            region: Name of ds9 region file (e.x. 'region.reg'). You can also pass a boolean mask array.
            bkg: Background Spectrum (1D numpy array; default None)
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis
            bayes_method: Bayesian Inference method. Options are '[emcee', 'dynesty'] (default 'emcee')
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            mean: Boolean to determine whether or not the mean spectrum is taken. This is used for calculating background spectra.
            nii_cons: Boolean to turn on or off NII doublet ratio constraint (default True)

        Return:
            X-axis and spectral axis of region.

        """
        # Create mask
        mask = None  # Initialize
        if '.reg' in region:
            shape = (2064, 2048)  # (self.header["NAXIS1"], self.header["NAXIS2"])  # Get the shape
            r = pyregion.open(region).as_imagecoord(self.header)  # Obtain pyregion region
            mask = r.get_mask(shape=shape).T  # Calculate mask from pyregion region
        elif '.npy' in region:
            mask = np.load(region)
        else:
            print("At the moment, we only support '.reg' and '.npy' files for masks.")
            print("Terminating Program!")
        # Set spatial bounds for entire cube
        x_min = 0
        x_max = self.cube_final.shape[0]
        y_min = 0
        y_max = self.cube_final.shape[1]
        integrated_spectrum = np.zeros(self.cube_final.shape[2])
        spec_ct = 0
        for i in tqdm(range(y_max - y_min)):
            y_pix = y_min + i
            for j in range(x_max - x_min):
                x_pix = x_min + j
                # Check if pixel is in the mask or not
                if mask[x_pix, y_pix]:
                    integrated_spectrum += self.cube_final[x_pix, y_pix, :]
                    spec_ct += 1
                else:
                    pass
        if mean:
            integrated_spectrum /= spec_ct  # Take mean spectrum
        if bkg is not None:
            integrated_spectrum -= bkg  # Subtract background spectrum
        good_sky_inds = [~np.isnan(integrated_spectrum)]  # Clean up spectrum
        sky = integrated_spectrum[good_sky_inds]
        axis = self.spectrum_axis[good_sky_inds]
        # Call fit!
        fit = Fit(sky, axis, self.wavenumbers_syn, fit_function, lines, vel_rel, sigma_rel,
                  self.model_ML, trans_filter=self.transmission_interpolated,
                  theta=self.interferometer_theta[x_pix, y_pix],
                  delta_x=self.hdr_dict['STEP'], n_steps=self.step_nb,
                  zpd_index=self.zpd_index,
                  filter=self.hdr_dict['FILTER'],
                  bayes_bool=bayes_bool, bayes_method=bayes_method,
                  uncertainty_bool=uncertainty_bool, nii_cons=nii_cons,
                  mdn=self.mdn)
        fit_dict = fit.fit()
        return axis, sky, fit_dict

    def create_snr_map(self, x_min=0, x_max=2048, y_min=0, y_max=2064, method=1, n_threads=2):
        """
        Create signal-to-noise ratio (SNR) map of a given region. If no bounds are given,
        a map of the entire cube is calculated.

        Args:
            x_min: Minimal X value (default 0)
            x_max: Maximal X value (default 2048)
            y_min: Minimal Y value (default 0)
            y_max: Maximal Y value (default 2064)
            method: Method used to calculate SNR (default 1; options 1 or 2)
            n_threads: Number of threads to use
        Return:
            snr_map: Signal-to-Noise ratio map

        """
        # Calculate bounds for SNR calculation
        # Step through spectra
        # SNR = np.zeros((x_max-x_min, y_max-y_min), dtype=np.float32).T
        SNR = np.zeros((2048, 2064), dtype=np.float32).T
        # start = time.time()
        # def SNR_calc(SNR, i):
        flux_min = 0;
        flux_max = 0;
        noise_min = 0;
        noise_max = 0  # Initializing bounds for flux and noise calculation regions
        if self.hdr_dict['FILTER'] == 'SN3':
            flux_min = 15150;
            flux_max = 15300;
            noise_min = 14250;
            noise_max = 14400
        elif self.hdr_dict['FILTER'] == 'SN2':
            flux_min = 19500;
            flux_max = 20750;
            noise_min = 18600;
            noise_max = 19000
        elif self.hdr_dict['FILTER'] == 'SN1':
            flux_min = 26550;
            flux_max = 27550;
            noise_min = 25300;
            noise_max = 25700
        else:
            print('SNR Calculation for this filter has not been implemented')

        def SNR_calc(i):
            y_pix = y_min + i
            snr_local = np.zeros(2048)
            for j in range(x_max - x_min):
                x_pix = x_min + j
                # Calculate SNR
                # Select spectral region around Halpha and NII complex
                min_ = np.argmin(np.abs(np.array(self.spectrum_axis) - flux_min))
                max_ = np.argmin(np.abs(np.array(self.spectrum_axis) - flux_max))
                in_region = self.cube_final[x_pix, y_pix, min_:max_]
                flux_in_region = np.nansum(self.cube_final[x_pix, y_pix, min_:max_])
                # Subtract off continuum estimate
                clipped_spec = astrostats.sigma_clip(self.cube_final[x_pix, y_pix, min_:max_], sigma=2, masked=False,
                                                     copy=False, maxiters=3)
                # Now take the mean value to serve as the continuum value
                cont_val = np.median(clipped_spec)
                flux_in_region -= cont_val * (max_ - min_)  # Need to scale by the number of steps along wavelength axis
                # Select distance region
                min_ = np.argmin(np.abs(np.array(self.spectrum_axis) - noise_min))
                max_ = np.argmin(np.abs(np.array(self.spectrum_axis) - noise_max))
                out_region = self.cube_final[x_pix, y_pix, min_:max_]
                std_out_region = np.nanstd(self.cube_final[x_pix, y_pix, min_:max_])
                if method == 1:
                    signal = np.nanmax(in_region) - np.nanmedian(in_region)
                    noise = np.abs(np.nanstd(out_region))
                    snr = float(signal / np.sqrt(noise))
                    if snr < 0:
                        snr = 0
                    else:
                        snr = snr / (np.sqrt(np.nanmean(np.abs(in_region))))
                else:
                    snr = float(flux_in_region / std_out_region)
                    if snr < 0:
                        snr = 0
                    else:
                        pass
                snr_local[x_pix] = snr
            return snr_local, i

        res = Parallel(n_jobs=n_threads, backend="threading")(delayed(SNR_calc)(i) for i in tqdm(range(y_max - y_min)));
        # Save
        for snr_ind in res:
            snr_vals, step_i = snr_ind
            SNR[y_min + step_i] = snr_vals
        fits.writeto(self.output_dir + '/' + self.object_name + '_SNR.fits', SNR, self.header, overwrite=True)

        # Save masks for SNr 3, 5, and 10
        for snr_val in [3, 5, 10]:
            mask = ma.masked_where(SNR >= snr_val, SNR)
            np.save("%s/SNR_%i_mask.npy" % (self.output_dir, snr_val), mask.mask)

        return None

    def update_astrometry(self, api_key):
        """
        Use astronomy.net to update the astrometry in the header using the deep image.
        If astronomy.net successfully finds the corrected astrononmy, the self.header is updated. Otherwise,
        the header is not updated and an exception is thrown.

        This automatically updates the deep images header! If you want the header to be binned, then you can bin it
        using the standard creation mechanisms (for this example binning at 2x2) and then run this code:

        >>> cube.create_deep_image(binning=2)
        >>> cube.update_astrometry(api_key)

        Args:
            api_key: Astronomy.net user api key
        """
        # Initiate Astronomy Net
        ast = AstrometryNet()
        ast.key = api_key
        ast.api_key = api_key
        try_again = True
        submission_id = None
        # Check that deep image exists. Otherwise make one
        if not os.path.exists(self.output_dir + '/' + self.object_name + '_deep.fits'):
            self.create_deep_image()
        # Now submit to astronomy.net until the value is found
        while try_again:
            if not submission_id:
                try:
                    wcs_header = ast.solve_from_image(self.output_dir + '/' + self.object_name + '_deep.fits',
                                                      submission_id=submission_id,
                                                      solve_timeout=300)  # , use_sextractor=True, center_ra=float(ra), center_dec=float(dec))
                except Exception as e:
                    print("Timedout")
                    submission_id = e.args[1]
                else:
                    # got a result, so terminate
                    print("Result")
                    try_again = False
            else:
                try:
                    wcs_header = ast.monitor_submission(submission_id, solve_timeout=300)
                except Exception as e:
                    print("Timedout")
                    submission_id = e.args[1]
                else:
                    # got a result, so terminate
                    print("Result")
                    try_again = False

        if wcs_header:
            # Code to execute when solve succeeds
            # update deep image header
            deep = fits.open(self.output_dir + '/' + self.object_name + '_deep.fits')
            deep[0].header.update(wcs_header)
            deep.close()
            # Update normal header
            self.header = wcs_header

        else:
            # Code to execute when solve fails
            print('Astronomy.net failed to solve. This astrometry has not been updated!')

    def heliocentric_correction(self):
        """
        Calculate heliocentric correction for observation given the location of SITELLE/CFHT
        and the time of the observation
        """
        CFHT = EarthLocation.of_site('CFHT')
        sc = SkyCoord(ra=self.hdr_dict['CRVAL1'] * u.deg, dec=self.hdr_dict['CRVAL2'] * u.deg)
        heliocorr = sc.radial_velocity_correction('heliocentric', obstime=Time(self.hdr_dict['DATE-OBS']),
                                                  location=CFHT)
        helio_kms = heliocorr.to(u.km / u.s)
        return helio_kms


    def skyline_calibration(self, n_grid, bin_size=30):
        """
        Compute skyline calibration by fitting the 6498.729 Angstrom line. Flexures
        of the telescope lead to minor offset that can be measured by high resolution
        spectra (R~5000). This function divides the FOV into a grid of NxN spaxel regions
        of 10x10 pixels to increase the signal. The function will output a map of the
        velocity offset. The initial velocity guess is set to 80 km/s. Additionally,
        we fit with a simple sinc function.

        Args:
            n_grid: NxN grid (int)
            bin_size: Size of grouping used for each region (optional int; default=30)

        Return:
            Velocity offset map
        """
        # Read in sky lines
        sky_lines_df = pd.read_csv('Data/sky_lines.csv', skiprows=2)
        sky_lines = sky_lines_df['Wavelength']  # Get wavelengths
        sky_lines = [sky_line/10 for sky_line in sky_lines]  # Convert from angstroms to nanometers
        # Create skyline dictionary
        sky_line_dict = {}  #  {OH_num: wavelength in nm}
        for line_ct, line_wvl in enumerate(sky_lines):
            sky_line_dict['OH_%i' % line_ct] = line_wvl
        # Calculate grid
        x_min = 0
        x_max = self.cube_final.shape[0]
        x_step = int((x_max - x_min) / n_grid)  # Calculate step size based on min and max values and the number of grid points
        y_min = 0
        y_max = self.cube_final.shape[1]
        y_step = int((y_max - y_min) / n_grid)  # Calculate step size based on min and max values and the number of grid points
        vel_grid = np.zeros((n_grid, n_grid))  # Initialize velocity grid
        for x_grid in range(n_grid):  # Step through x steps
            for y_grid in range(n_grid):  # Step through y steps
                # Collect spectrum in 10x10 region
                x_center = x_min + int(0.5 * (x_step) * (x_grid + 1))
                y_center = y_min + int(0.5 * (y_step) * (y_grid + 1))
                integrated_spectrum = np.zeros_like(self.cube_final[x_center, y_center, :])  # Initialize as zeros
                for i in range(bin_size):  # Take bin_size x bin_size bins
                    for j in range(bin_size):
                        integrated_spectrum += self.cube_final[x_center+i, y_center+i, :]
                # Collapse to single spectrum
                good_sky_inds = [~np.isnan(integrated_spectrum)]  # Clean up spectrum
                sky = integrated_spectrum[good_sky_inds]
                axis = self.spectrum_axis[good_sky_inds]
                # Call fit!
                fit = Fit(sky, axis, self.wavenumbers_syn, 'sinc', ['OH_%i' % num for num in len(sky_lines)], [1], [1],
                          self.model_ML, trans_filter=self.transmission_interpolated,
                          theta=self.interferometer_theta[x_center, y_center],
                          delta_x=self.hdr_dict['STEP'], n_steps=self.step_nb,
                          zpd_index=self.zpd_index,
                          filter=self.hdr_dict['FILTER'], bayes_bool=True, bayes_method='emcee', sky_lines=sky_line_dict
                          )

                velocity, fit_vector = fit.fit(sky_line=True)
                vel_grid[x_grid, y_grid] = float(velocity)
        # Now that we have the grid, we need to reproject it onto the original pixel grid
        print(vel_grid)
        vel_grid_final = np.zeros((x_max, y_max))
        for x_grid in range(n_grid):  # Step through x steps
            for y_grid in range(n_grid):  # Step through y steps
                # Collect spectrum in 10x10 region
                x_center = x_min + int(0.5 * (x_step) * (x_grid + 1))
                y_center = y_min + int(0.5 * (y_step) * (y_grid + 1))
                vel_grid_final[x_center - x_step:x_center + x_step, y_center - y_step:y_center + y_step] = vel_grid[
                    x_grid, y_grid]
        fits.writeto(self.output_dir + '/velocity_correction.fits', vel_grid, self.header, overwrite=True)

    def create_component_map(self, x_min=0, x_max=2048, y_min=0, y_max=2064, bkg=None, n_threads=2):
        """
        Create component map of a given region following our third paper. If no bounds are given,
        a map of the entire cube is calculated.

        Args:
            x_min: Minimal X value (default 0)
            x_max: Maximal X value (default 2048)
            y_min: Minimal Y value (default 0)
            y_max: Maximal Y value (default 2064)
            bkg: Background Spectrum (1D numpy array; default None)
            n_threads: Number of threads to use
        Return:
            component_map: component map

        """
        # Calculate bounds for SNR calculation
        # Step through spectra
        Comps = np.zeros((2048, 2064), dtype=np.float32).T
        Preds = np.zeros((2048, 2064), dtype=np.float32).T
        if self.hdr_dict['FILTER'] == 'SN3':
            # Read in machine learning algorithm
            comps_model = keras.models.load_model(
                self.Luci_path + 'ML/R%i-COMPONENTS-%s.h5' % (self.resolution, self.filter))
        else:
            print('Component Calculation has only been implemented in SN3!')
            print('Terminating program!')
            exit()

        def component_calc(i):
            y_pix = y_min + i
            comps_local = np.zeros(2048)
            preds_local = np.zeros(2048)
            for j in range(x_max - x_min):
                x_pix = x_min + j
                # Calculate how many components are present in SN3 using a convolutional neural network
                sky = self.cube_final[x_pix, y_pix, :]
                good_sky_inds = [~np.isnan(sky)]  # Clean up spectrum
                if bkg is not None:
                    sky = sky[good_sky_inds] - bkg[good_sky_inds]
                else:
                    sky = sky[good_sky_inds]
                axis = self.spectrum_axis[good_sky_inds]
                # Interpolate
                f = interpolate.interp1d(axis, sky, kind='slinear', fill_value='extrapolate')
                spectrum_interpolated = f(self.wavenumbers_syn_full[2:-2])
                spectrum_scaled = spectrum_interpolated / np.max(spectrum_interpolated)
                Spectrum = spectrum_scaled.reshape(1, spectrum_scaled.shape[0], 1)
                predictions = comps_model(Spectrum, training=False)
                max_ind = np.argmax(predictions[0])  # ID of outcome (0 -> single; 1 -> double)
                comp = max_ind + 1  # 1 -> single; 2 -> double
                comps_local[x_pix] = comp
                #print(predictions)
                preds_local[x_pix] = predictions[0][comp-1]
            return comps_local, preds_local, i

        res = Parallel(n_jobs=n_threads, backend="threading")(
            delayed(component_calc)(i) for i in tqdm(range(y_max - y_min)))
        # Save
        for comp_ind in res:
            comp_vals, preds_vals, step_i = comp_ind
            Comps[y_min + step_i] = comp_vals
            Preds[y_min + step_i] = preds_vals

        fits.writeto(self.output_dir + '/' + self.object_name + '_comps.fits', Comps, self.header, overwrite=True)
        fits.writeto(self.output_dir + '/' + self.object_name + '_comps_probs.fits', Preds, self.header, overwrite=True)

    def close(self):
        """
        Functionality to delete Luci object (and thus the cube) from memory
        """
        del self.cube_final
        del self.header
        if self.cube_binned:
            del self.cube_binned

    def check_luci_path(self):
        """
        Functionality to check that the user has included the trailing "/" to Luci_path.
        If they have not, we add it.
        TODO: TEST
        """
        if not self.Luci_path.endswith('/'):
            self.Luci_path += '/'
            print("We have added a trailing '/' to your Luci_path variable.\n")
            print("Please add this in the future.\n")

    def create_wvt(self, x_min_init, x_max_init, y_min_init, y_max_init, pixel_size, StN_target, roundness_crit, ToL):
        """
        """
        print("#----------------WVT Algorithm----------------#")
        Pixels = []
        self.create_snr_map(x_min_init, x_max_init, y_min_init, y_max_init, method=2, n_threads=1)
        print("#----------------Algorithm Part 1----------------#")
        SNR_map = fits.open(self.output_dir+'/'+self.object_name+'_SNR.fits')[0].data
        SNR_map = SNR_map[y_min_init:y_max_init,x_min_init:x_max_init]
        fits.writeto(self.output_dir+'/'+self.object_name+'_SNR.fits', SNR_map, overwrite=True)
        Pixels, x_min, x_max, y_min, y_max = read_in(self.output_dir+'/'+self.object_name+'_SNR.fits')
        Nearest_Neighbors(Pixels)
        Init_bins = Bin_Acc(Pixels, pixel_size, StN_target, roundness_crit)
        plot_Bins(Init_bins, x_min, x_max, y_min, y_max, StN_target, self.output_dir, "bin_acc")
        print("#----------------Algorithm Part 2----------------#")
        Final_Bins = WVT(Init_bins, Pixels, StN_target, ToL, pixel_size, self.output_dir)
        print("#----------------Algorithm Complete--------------#")
        plot_Bins(Final_Bins, x_min, x_max, y_min, y_max, StN_target, self.output_dir,"final")
        Bin_data(Final_Bins, Pixels, x_min, y_min, self.output_dir, "WVT_data")
        print("#----------------Bin Mapping--------------#")
        pixel_x = []
        pixel_y = []
        bins = []
        bin_map = np.zeros((x_max-x_min, y_max-y_min))
        j = 0
        i = 0
        with open (self.output_dir+'/WVT_data.txt', 'rt') as myfile:
            myfile = myfile.readlines()[3:]
            for myline in myfile:
                myline = myline.strip(' \n')
                data = [int(s) for s in myline.split() if s.isdigit()]
                pixel_x.append(data[0])
                pixel_y.append(data[1])
                bins.append(data[2])
        for pix_x, pix_y in zip(pixel_x,pixel_y):
            bin_map[pix_x,pix_y] = int(bins[i])
            i += 1
        #bin_map = np.rot90(bin_map)
        print("#----------------Numpy Bin Mapping--------------#")
        if not os.path.exists(self.output_dir+'/Numpy_Voronoi_Bins'):
            os.mkdir(self.output_dir+'/Numpy_Voronoi_Bins')
        if os.path.exists(self.output_dir+'/Numpy_Voronoi_Bins'):
            files = glob.glob(self.output_dir+'/Numpy_Voronoi_Bins/*.npy')
            for f in files:
                os.remove(f)
        for bin_num in list(range(len(Final_Bins))):
            print("We're at bin number : ", bin_num)
            bool_bin_map = np.zeros((2048, 2064), dtype=bool)
            for a,b in zip(np.where(bin_map == bin_num)[0][:],np.where(bin_map == bin_num)[1][:]):
                bool_bin_map[x_min_init + a,y_min_init + b] = True
            np.save(self.output_dir+'/Numpy_Voronoi_Bins/bool_bin_map_%i'%j, bool_bin_map)
            j+=1

    def fit_wvt(self, lines, fit_function, vel_rel, sigma_rel, bkg=None, bayes_bool=False, uncertainty_bool=False, mean=False, n_threads=1):
        x_min = 0
        x_max = self.cube_final.shape[0]
        y_min = 0
        y_max = self.cube_final.shape[1]
        chi2_fits = np.zeros((x_max-x_min, y_max-y_min), dtype=np.float32).T
        corr_fits = np.zeros((x_max-x_min, y_max-y_min), dtype=np.float32).T
        step_fits = np.zeros((x_max-x_min, y_max-y_min), dtype=np.float32).T
        # First two dimensions are the X and Y dimensions.
        #The third dimension corresponds to the line in the order of the lines input parameter.
        ampls_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        flux_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        flux_errors_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        velocities_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        broadenings_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        velocities_errors_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        broadenings_errors_fits = np.zeros((x_max-x_min, y_max-y_min, len(lines)), dtype=np.float32).transpose(1,0,2)
        continuum_fits = np.zeros((x_max-x_min, y_max-y_min), dtype=np.float32).T
        ct = 0
        set_num_threads(n_threads)
        if not os.path.exists(self.output_dir+'/'+self.object_name+'_deep.fits'):
            self.create_deep_image()
        wcs = WCS(self.header, naxis=2)
        cutout = Cutout2D(fits.open(self.output_dir+'/'+self.object_name+'_deep.fits')[0].data, position=((x_max+x_min)/2, (y_max+y_min)/2), size=(x_max-x_min, y_max-y_min), wcs=wcs)
        for bin_num in list(range(len(os.listdir(self.output_dir+'/Numpy_Voronoi_Bins/')))):
            print("We're at bin number : ", bin_num)
            bool_bin_map = self.output_dir+'/Numpy_Voronoi_Bins/bool_bin_map_%i.npy'%bin_num
            bin_axis, bin_sky, bin_fit_dict = self.fit_spectrum_region(lines, fit_function, vel_rel, sigma_rel, region= bool_bin_map, bkg=bkg, bayes_bool=False, uncertainty_bool=False, mean=True)
            index = np.where(np.load(bool_bin_map) == True)
            for a, b in zip(index[0], index[1]):
                ampls_fits[a,b] = bin_fit_dict['amplitudes']
                flux_fits[a,b] = bin_fit_dict['fluxes']
                flux_errors_fits[a,b] = bin_fit_dict['flux_errors']
                broadenings_fits[a,b] = bin_fit_dict['sigmas']
                broadenings_errors_fits[a,b] = bin_fit_dict['sigmas_errors']
                chi2_fits[a,b] = bin_fit_dict['chi2']
                continuum_fits[a,b] = bin_fit_dict['continuum']
                velocities_fits[a,b] = bin_fit_dict['velocities']
                velocities_errors_fits[a,b] = bin_fit_dict['vels_errors']
        self.save_fits(lines, ampls_fits, flux_fits, flux_errors_fits, velocities_fits, broadenings_fits, velocities_errors_fits, broadenings_errors_fits, chi2_fits, continuum_fits, cutout.wcs.to_header(), binning = 1)
        return velocities_fits, broadenings_fits, flux_fits, chi2_fits
