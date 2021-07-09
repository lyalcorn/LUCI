import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize
from scipy import interpolate
import keras
from scipy.optimize import Bounds
from numdifftools import Jacobian, Hessian
import emcee
from scipy.stats import chisquare
from scipy import special as sps
import warnings

warnings.filterwarnings("ignore")


class Gaussian:
    def __init__(self, channel, params):
        A = params[0];
        x = params[1];
        sigma = params[2]
        self.func = A * np.exp((-(channel - x) ** 2) / (2 * sigma ** 2))


class Sinc:
    def __init__(self, channel, params):
        p0 = params[0];
        p1 = params[1];
        p2 = params[2]
        u = (channel - p1) / p2
        self.func = p0 * (np.sin(u) / u)


class SincGauss:
    def __init__(self, channel, params, sinc_width):
        p0 = params[0]
        p1 = params[1]
        p2 = sinc_width
        p3 = params[2]
        a = p3/(np.sqrt(2)*p2)
        b = (channel-p1)/(np.sqrt(2)*p3)
        self.func = p0*np.exp(-b**2)*((sps.erf(a-1j*b)+sps.erf(a+1j*b))/(2*sps.erf(a)))


class Fit:
    """
    Class that defines the functions necessary for the modelling aspect. This includes
    the gaussian fit functions, the prior definitions, the log likelihood, and the
    definition of the posterior (log likelihood times prior).

    The initial arguments are as follows:
    Args:

        spectrum: Spectrum of interest. This should not be the interpolated spectrum nor normalized(numpy array)

        axis: Wavelength Axis of Spectrum (numpy array)

        wavenumbers_syn: Wavelength Axis of Reference Spectrum (numpy array)

        model_type: Type of model ('gaussian')

        lines: Lines to fit (must be in line_dict)

        sigma_rel: Constraints on sigma (must be list)

        ML_model: Tensorflow/keras machine learning model

        Plot_bool: Boolean to determine whether or not to plot the spectrum (default = False)

    """

    def __init__(self, spectrum, axis, wavenumbers_syn, model_type, lines, vel_rel, sigma_rel,
                 ML_model, theta=0, delta_x=2, n_steps=842, bayes_bool=False, Plot_bool=False):
        """
        Args:
            spectrum: Spectrum of interest. This should not be the interpolated spectrum nor normalized(numpy array)
            axis: Wavelength Axis of Spectrum (numpy array)
            wavenumbers_syn: Wavelength Axis of Reference Spectrum (numpy array)
            model_type: Type of model ('gaussian')
            lines: Lines to fit (must be in line_dict)
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            ML_model: Tensorflow/keras machine learning model
            theta: Interferometric angle in degrees (defaults to 11.960 -- this is so that the correction coeff is 1)
            delta_x: Step Delta
            n_steps: Number of steps in spectra
            bayes_bool:
            Plot_bool: Boolean to determine whether or not to plot the spectrum (default = False)

        """
        self.line_dict = {'Halpha': 656.280, 'NII6583': 658.341, 'NII6548': 654.803,
                          'SII6716': 671.647, 'SII6731': 673.085, 'OII3726': 372.603,
                          'OII3729': 372.882, 'OIII4959': 495.891, 'OIII5007': 500.684,
                          'Hbeta': 486.133}
        self.available_functions = ['gaussian', 'sinc', 'sincgauss']
        self.spectrum = spectrum
        self.axis = axis
        self.wavenumbers_syn = wavenumbers_syn
        self.model_type = model_type
        self.lines = lines
        self.line_num = len(lines)  # Number of  lines to fit
        self.spectrum_interpolated = np.zeros_like(self.spectrum)
        self.spectrum_normalized = self.spectrum / np.max(self.spectrum)  # Normalized spectrum
        self.spectrum_interp_norm = np.zeros_like(self.spectrum)
        self.theta = theta
        self.cos_theta = np.cos(self.theta)
        self.correction_factor = 1.0  # Initialize Correction factor
        self.axis_step = delta_x  # Initialize
        self.delta_x = delta_x
        self.n_steps = n_steps
        self.calculate_correction()
        self.sigma_rel = sigma_rel
        self.vel_rel = vel_rel
        # ADD ML_MODEL AND PLOT_BOOL
        self.ML_model = ML_model
        self.bayes_bool = bayes_bool
        self.Plot_bool = Plot_bool
        self.spectrum_scale = 0.0  # Sacling factor used to normalize spectrum
        self.sinc_width = 0.0  # Width of the sinc function -- Initialize to zero
        #if sincgauss_args is None:
        #    sincgauss_args = [11.96, 2.1, 892]  # Randomly initialize these values  # TODO: Look niito best values
        self.calc_sinc_width([self.cos_theta, self.delta_x, self.n_steps])
        self.vel_ml = 0.0  # ML Estimate of the velocity [km/s]
        self.broad_ml = 0.0  # ML Estimate of the velocity dispersion [km/s]
        self.fit_sol = np.zeros(3 * self.line_num)  # Solution to the fit
        # Set bounds
        self.A_min = 0;
        self.A_max = 1.1;
        self.x_min = 0 #  14700;
        self.x_max = 1e8 #  15600
        self.sigma_min = 0.01;
        self.sigma_max = 10

        # Check that lines inputted by user are in line_dict
        self.check_lines()
        self.check_fitting_model()


    def calculate_correction(self):
        """
        Calculate correction factor based of interferometric angle. This is used to correct the broadening
        """
        self.correction_factor = 1/self.cos_theta
        self.axis_step = self.correction_factor / (2*self.delta_x*self.n_steps) * 1e7



    def calc_sinc_width(self, sincgauss_args):
        """
        Calculate sinc width of the sincgauss function
        Args:
            sincgauss_args: Additional arguments required for sincgauss function in a list:
            [Cosine of the Interfermeter Angle as calculated in Luci.get_interferometer_angle(), step_delta, n_steps]

        """
        MPD = sincgauss_args[0]*sincgauss_args[1]*sincgauss_args[2]
        self.sinc_width = 1/(2*MPD)


    def estimate_priors_ML(self):
        """
        TODO: Test
        Apply machine learning algorithm on spectrum in order to estimate the velocity.
        The spectrum fed into this method must be interpolated already onto the
        reference spectrum axis AND normalized as described in Rhea et al. 2020a.
        Args:
            ml_dir: Relative path to the trained ML Predictor (e.g. R5000-PREDICITOR-I)
        Return:
            Updates self.vel_ml
        """
        Spectrum = self.spectrum_interp_norm.reshape(1, self.spectrum_interp_norm.shape[0], 1)
        predictions = self.ML_model(Spectrum, training=False)
        self.vel_ml = float(predictions[0][0])
        self.broad_ml = float(predictions[0][1])  # Multiply value by FWHM of a gaussian
        return None

    def interpolate_spectrum(self):
        """
        Interpolate Spectrum given the wavelength axis of reference spectrum.
        Then normalize the spectrum so that the max value equals 1

        Return:
            Populates self.spectrum_interpolated, self.spectrum_scale, and self.spectrum_interp_norm.

        """
        f = interpolate.interp1d(self.axis, self.spectrum, kind='slinear')
        self.spectrum_interpolated = f(self.wavenumbers_syn)
        self.spectrum_scale = np.max(self.spectrum_interpolated)
        self.spectrum_interp_norm = self.spectrum_interpolated / self.spectrum_scale
        # self.spectrum_interpolated = np.real(sky_corr)
        return None

    def line_vals_estimate(self, line_name):
        """
        TODO: Test

        Function to estimate the position and amplitude of a given line.

        Args:
            spec: Spectrum flux values
            line_name: Name of model. Available options are 'Halpha', 'NII6548', 'NII6543', 'SII6716', 'SII6731'

        Return:
            Estimated line amplitude in units of cm-1 (line_amp_est) and estimate line position in units of cm-1 (line_pos_est)

        """
        line_theo = self.line_dict[line_name]
        if self.ML_model is None or self.model_type == '':
            max_flux = np.argmax(self.spectrum_normalized)
            self.vel_ml = np.abs(3e5 * ((1e7/self.axis[max_flux] - line_theo) / line_theo))
            self.broad_ml = 10.0  # Best for now
        else:
            pass  # vel_ml and broad_ml already set using ML algorithm
        line_pos_est = 1e7 / ((self.vel_ml / 3e5) * line_theo + line_theo)  # Estimate of position of line in cm-1
        line_ind = np.argmin(np.abs(np.array(self.axis) - line_pos_est))
        line_amp_est = np.max([self.spectrum_normalized[line_ind - 2], self.spectrum_normalized[line_ind - 1],
                               self.spectrum_normalized[line_ind], self.spectrum_normalized[line_ind + 1],
                               self.spectrum_normalized[line_ind + 2]])
        line_broad_est = (line_pos_est * self.broad_ml * self.correction_factor) / 3e5
        return line_amp_est, line_pos_est, line_broad_est

    def gaussian_model(self, channel, theta):
        """
        Function to initiate the correct number of models to fit

        Args:
            channel: Wavelength Axis in cm-1
            theta: List of parameters for all the models in the following order
                            [amplitude, line location, sigma]

        Return:
            Value of function given input parameters (theta)

        """
        f1 = 0.0
        for model_num in range(self.line_num):
            params = theta[model_num * 3:(model_num + 1) * 3]
            f1 += Gaussian(channel, params).func
        return f1


    def sinc_model(self, channel, theta):
        """
        Function to initiate the correct number of models to fit

        Args:
            channel: Wavelength Axis in cm-1
            theta: List of parameters for all the models in the following order
                            [amplitude, line location, sigma]

        Return:
            Value of function given input parameters (theta)

        """
        f1 = 0.0
        for model_num in range(self.line_num):
            params = theta[model_num * 3:(model_num + 1) * 3]
            f1 += Sinc(channel, params).func
        return f1


    def sincgauss_model(self, channel, theta):
        """
        Function to initiate the correct number of models to fit

        Args:
            channel: Wavelength Axis in cm-1
            theta: List of parameters for all the models in the following order
                            [amplitude, line location, sigma]

        Return:
            Value of function given input parameters (theta)

        """
        f1 = 0.0
        for model_num in range(self.line_num):
            params = theta[model_num * 3:(model_num + 1) * 3]
            f1 += SincGauss(channel, params, self.sinc_width).func
        return np.real(f1)


    def log_likelihood(self, theta, yerr):
        """
        Calculate log likelihood function evaluated given parameters on spectral axis

        Args:
            theta - List of parameters for all the models in the following order
                            [amplitude, line location, sigma]
            yerr: Error on Spectrum's flux values (default 1e-2)
        Return:
            Value of log likelihood

        """
        if self.model_type == 'gaussian':
            model = self.gaussian_model(self.axis, theta)
        elif self.model_type == 'sinc':
            model = self.sinc_model(self.axis, theta)
        elif self.model_type == 'sincgauss':
            model = self.sincgauss_model(self.axis, theta)
        sigma2 = yerr ** 2
        return -0.5 * np.sum((self.spectrum_normalized - model) ** 2 / sigma2 + np.log(2 * np.pi * sigma2))

    def fun_der(self, theta, yerr):
        return Jacobian(lambda theta: self.log_likelihood(theta, yerr))(theta).ravel()


    def sigma_constraints(self):
        """
        Set up constraints for sigma values before fitting line
        Return:
            Dictionary describing constraints
        """
        sigma_dict_list = []
        unique_rels = np.unique(self.sigma_rel)  # List of unique groups
        for unique_ in unique_rels:  # Step through each unique group
            inds_unique = [i for i, e in enumerate(self.sigma_rel) if e == unique_]  # Obtain line indices in group
            if len(inds_unique) > 1:  # If there is more than one element in the group
                ind_0 = inds_unique[0]  # Get first element
                for ind_unique in inds_unique[1:]:  # Step through group elements except for the first one
                    sigma_dict_list.append({'type': 'eq', 'fun': lambda x: x[3*ind_0+2] - x[3*ind_unique+2]})
        return sigma_dict_list

    def vel_constraints(self):
        """
        Set up constraints for velocity values before fitting line
        Return:
            Dictionary describing constraints
        """
        vel_dict_list = []
        unique_rels = np.unique(self.vel_rel)  # List of unique groups
        for unique_ in unique_rels:  # Step through each unique group
            inds_unique = [i for i, e in enumerate(self.vel_rel) if e == unique_]  # Obtain line indices in group
            if len(inds_unique) > 1:  # If there is more than one element in the group
                ind_0 = inds_unique[0]  # Get first element
                for ind_unique in inds_unique[1:]:  # Step through group elements except for the first one
                    expr_dict = {'type': 'eq',
                             'fun': lambda x: 3e5 * ((1e7 / x[3*ind_unique+1] - self.line_dict.values()[3*ind_unique+1]) / (1e7 / x[3*ind_unique+1])) - 3e5 * (
                                     (1e7 / x[3*ind_0+1] - self.line_dict.values()[3*ind_0+1]) / (1e7 / x[3*ind_0+1]))}
        return vel_dict_list


    def calculate_params(self):
        """
        Calculate the amplitude, position, and sigma of the line. These values are
        calculated using the scipy.optimize.minimize function. This is called
        on the log likelood previously described. The minimization algorithm uses
        the SLSQP optimization implementation. We have applied standard bounds in order
        to speed up the fitting. We also apply the fit on the normalized spectrum.
        We then correct the flux by un-normalizing the spectrum.

        """
        nll = lambda *args: -self.log_likelihood(*args)
        initial = np.ones((3 * self.line_num))
        bounds_ = []
        for mod in range(self.line_num):
            val = 3 * mod + 1
            amp_est, vel_est, sigma_est = self.line_vals_estimate(self.lines[mod])
            initial[3 * mod] = amp_est
            initial[3 * mod + 1] = vel_est
            initial[3 * mod + 2] = sigma_est
            bounds_.append((self.A_min, self.A_max))
            bounds_.append((self.x_min, self.x_max))
            bounds_.append((self.sigma_min, self.sigma_max))
        bounds_l = [val[0] for val in bounds_]
        bounds_u = [val[1] for val in bounds_]
        bounds = Bounds(bounds_l, bounds_u)
        self.inital_values = initial
        sigma_cons = self.sigma_constraints()
        vel_cons = self.vel_constraints()
        cons = (sigma_cons + vel_cons)
        soln = minimize(nll, initial, method='SLSQP',# jac=self.fun_der(),
                        options={'disp': False, 'maxiter': 1000}, bounds=bounds, tol=1e-8,
                        args=(1e-2), constraints=cons)
        parameters = soln.x
        # We now must unscale the amplitude
        for i in range(self.line_num):
            parameters[i * 3] *= self.spectrum_scale
        self.fit_sol = parameters
        self.fit_vector = self.gaussian_model(self.axis, self.fit_sol)
        return None


    def calculate_vel(self, ind):
        """
        Calculate velocity given the fit of Halpha
        TODO: Test
        TODO: Add other lines

        Args:
            ind: Index of line in lines
        Return:
            Velocity of the Halpha line in units of km/s
        """
        line_name = self.lines[ind]
        l_calc = 1e7 / self.fit_sol[3*ind+1]  # Halpha
        l_shift = (l_calc - self.line_dict[line_name]) / l_calc
        v = 3e5 * l_shift
        return v

    def calculate_broad(self, ind):
        """
        Calculate velocity dispersion given the fit of Halpha
        TODO: Test
        TODO: Add other lines

        Return:
            Velocity Dispersion of the Halpha line in units of km/s
        """
        broad = (3e5 * self.fit_sol[3*ind+2] * self.axis_step) / self.fit_sol[3*ind+1]
        return broad

    def calculate_flux(self, line_amp, line_sigma):
        """
        Calculate flux value given fit of line
        TODO: Test

        Args:
            line_amp: Amplitude of the line (un-normalized)
            line_sigma: Sigma of the line fit
        Return:
            Flux of the provided line in units of erg/s/cm-2
        """
        flux = 0.0  # Initialize
        if self.model_type == 'gaussian':
            flux = np.sqrt(2 * np.pi) * line_amp * line_sigma
        elif self.model_type == 'sinc':
            flux = np.sqrt(np.pi) * line_amp * line_sigma
        elif self.model_type == 'sincgauss':
            flux = line_amp * ((np.sqrt(2*np.pi)*line_sigma)/(sps.erf((line_sigma)/(np.sqrt(2)*self.sinc_width))))
        else:
            print("ERROR: INCORRECT FIT FUNCTION")
        return flux

    def fit(self):
        """
        Primary function call for a spectrum. This will estimate the velocity using
        our machine learning algorithm described in Rhea et al. 2020a. Then we will
        fit our lines using scipy.optimize.minimize.

        Return:
            dictionary of parameters returned by the fit. The dictionary has the following form:
            {"fit_vector": Fitted spectrum, "velocity": Velocity of the line in km/s (float),
            "broadening": Velocity Dispersion of the line in km/s (float)}
        """
        if self.ML_model != None:
            # Interpolate Spectrum
            self.interpolate_spectrum()
            # Estimate the priors using machine learning algorithm
            self.estimate_priors_ML()
        else:
            self.spectrum_scale = np.max(self.spectrum)
        # Apply Fit
        self.calculate_params()
        # Check if Bayesian approach is required
        if self.bayes_bool == True:
            self.fit_Bayes()
        # Calculate fit statistic
        chi_sqr, p_val = chisquare(self.fit_vector, self.spectrum)
        # Collect Amplitudes
        ampls = []
        fluxes = []
        vels = []
        sigmas = []
        for line_ct, line_ in enumerate(self.lines):  # Step through each line
            ampls.append(self.fit_sol[line_ct * 3])
            # Calculate flux
            fluxes.append(self.calculate_flux(self.fit_sol[line_ct * 3], self.fit_sol[line_ct * 3 + 2]))
            vels.append(self.calculate_vel(line_ct))
        # Collect parameters to return in a dictionary
        fit_dict = {'fit_sol': self.fit_sol, 'fit_vector': self.fit_vector,
                    'velocity': self.calculate_vel(0), 'broadening': self.calculate_broad(0),
                    'amplitudes': ampls, 'fluxes': fluxes, 'chi2': chi_sqr, 'velocities': vels,
                    'sigmas': sigmas}
        # Plot
        if self.Plot_bool == True:
            self.plot()
        return fit_dict

    def fit_Bayes(self):
        """
        Apply Bayesian MCMC run to constrain the parameters after solving
        """
        # Unscale the amplitude
        for i in range(self.line_num):
            self.fit_sol[i * 3] /= self.spectrum_scale
        n_dim = 3 * self.line_num
        n_walkers = n_dim * 2 + 4
        init_ = self.fit_sol + 1 * np.random.randn(n_walkers, n_dim)
        sampler = emcee.EnsembleSampler(n_walkers, n_dim, self.log_probability,
                                        args=(self.axis, self.spectrum_normalized, 1e-2, self.lines))
        sampler.run_mcmc(init_, 100, progress=False)
        flat_samples = sampler.get_chain(discard=20, flat=True)
        parameters = []
        for i in range(n_dim):
            mcmc = np.percentile(flat_samples[:, i], [16, 50, 84])
            parameters.append(mcmc[1])
        self.fit_sol = parameters
        # Now rescale the amplitude
        for i in range(self.line_num):
            self.fit_sol[i * 3] *= self.spectrum_scale

    def log_likelihood_bayes(self, theta, x, y, yerr, model__):
        """
        """
        # model = self.gaussian_model(x, theta, model)
        model = self.gaussian_model(self.axis, theta)
        sigma2 = yerr ** 2
        return -0.5 * np.sum((y - model) ** 2 / sigma2 + np.log(2 * np.pi * sigma2))

    def log_prior(self, theta, model):
        A_min = 0  # 1e-19
        A_max = 1.1  # 1e-15
        x_min = 0#14700
        x_max = 1e7#15400
        sigma_min = 0
        sigma_max = 10
        for model_num in range(len(model)):
            params = theta[model_num * 3:(model_num + 1) * 3]
        within_bounds = True  # Boolean to determine if parameters are within bounds
        for ct, param in enumerate(params):
            if ct % 3 == 0:  # Amplitude parameter
                if param > A_min and param < A_max:
                    pass
                else:
                    within_bounds = False  # Value not in bounds
                    break
            if ct % 3 == 1:  # velocity parameter
                if param > x_min and param < x_max:
                    pass
                else:
                    within_bounds = False  # Value not in bounds
                    break
            if ct % 3 == 2:  # sigma parameter
                if param > sigma_min and param < sigma_max:
                    pass
                else:
                    within_bounds = False  # Value not in bounds
                    break
        if within_bounds:
            return 0.0
        else:
            return -np.inf
        # A_,x_,sigma_ = theta
        # if A_min < A_ < A_max and x_min < x_ < x_max and sigma_min < sigma_ < sigma_max:
        #    return 0.0#np.log(1/((t_max-t_min)*(rp_max-rp_min)*(b_max-b_min)))
        # return -np.inf

    def log_probability(self, theta, x, y, yerr, model):
        lp = self.log_prior(theta, model)
        if not np.isfinite(lp):
            return -np.inf
        return lp + self.log_likelihood_bayes(theta, x, y, yerr, model)

    def plot(self):
        """
        Plot initial spectrum and fitted spectrum

        """
        plt.clf()
        plt.plot(self.axis, self.sky, label='Spectrum')
        plt.plot(self.axis, self.fit_vector, label='Fit')
        plt.legend()
        plt.show()
        return None

    def check_lines(self):
        """
        This function checks to see that the lines provided are in the available options
        Return:
        Nothing if the user provides appropriate lines
        Else it will throw an error

        """
        if set(self.lines).issubset(self.line_dict):
            pass
        else:
            raise Exception('Please submit a line name in the available list: \n {}'.format(self.line_dict.keys()))

    def check_fitting_model(self):
        """
        This function checks to see that the model provided is in the available options
        Return:
        Nothing if the user provides an appropriate fitting model
        Else it will throw an error

        """
        if self.model_type in self.available_functions:
            pass
        else:
            print(self.model_type)
            raise Exception(
                'Please submit a fitting function name in the available list: \n {}'.format(self.available_functions))
