from pprint import pformat

import numpy as np
from multistate_kernel import MultiStateKernel
from scipy import optimize
from sklearn.gaussian_process import GaussianProcessRegressor


def _tri_matrix_to_flat(matrix):
    return matrix[np.tril_indices_from(matrix)]


class FitFailedError(RuntimeError):
    pass


class GPInterpolator(object):
    """Interpolate light curve using multi-state Gaussian Process

    Parameters
    ----------
    curve: MultiStateData
    kernels: tuple of sklearn.gaussian_process kernels, size is len(curve)
    constant_matrix: matrix, shape is (len(curves), len(curves))
    constant_matrix_bounds: pair of matrices
    optimize_method: str or None, optional
        Optimize method name, should be valid `scipy.optimize.minimize` method
        with a support of constraints and hessian update strategy. The default
        is None, the default value of `optimizer` argument of
        `sklearn.gaussian_process.GaussianProcessRegressor` will be used
    n_restarts_optimizer: int, optional
    random_state: int or RandomState or None, optional

    Raises
    ------
    FitFailedError
    """
    def __init__(self, curve,
                 kernels, constant_matrix, constant_matrix_bounds,
                 optimize_method=None, n_restarts_optimizer=0,
                 random_state=None, add_err=0, raise_on_bounds=True):
        self.curve = curve
        self.n_restarts_optimizer = n_restarts_optimizer
        self.random_state = random_state
        self.kernel = MultiStateKernel(kernels, constant_matrix, constant_matrix_bounds)
        if optimize_method is None:
            self.optimizer = 'fmin_l_bfgs_b'  # the default for scikit-learn 0.19
        else:
            self.optimizer = self.optimizer(optimize_method)
        self.regressor = GaussianProcessRegressor(self.kernel, alpha=curve.arrays.err**2 + curve.arrays.y**2*(add_err/100)**2,
                                                  optimizer=self.optimizer,
                                                  n_restarts_optimizer=self.n_restarts_optimizer,
                                                  normalize_y=True, random_state=self.random_state)
        self.regressor.fit(curve.arrays.x, curve.arrays.y)
        if raise_on_bounds:
            if self.is_near_bounds(self.regressor.kernel_):
                raise FitFailedError(
                    '''Fit was not succeed, some of the values are near bounds. Resulted kernel is
                    {}'''.format(pformat(self.regressor.kernel_.get_params()))
                )

    def __call__(self, x, compute_err=True):
        """Produce median and std of GP realizations

        Parameters
        ----------
        x: array-like, shape = (n,)
        compute_err: bool, optional

        Returns
        -------
        MultiStateData
        """
        x = self.curve.sample(x)
        if compute_err:
            y, err = self.regressor.predict(x, return_std=True)
        else:
            y = self.regressor.predict(x)
            err = np.full_like(y, np.nan)
        return self.curve.convert_arrays(x, y, err)

    def y_samples(self, x, samples=1, random_state=None):
        """Generate GP realizations

        Parameters
        ----------
        x: array-like, shape = (n,)
        samples: int, optional
            Number of samples to generate. If larger than 0, additional tuple
            of samples will be returned
        random_state: int or RandomState or None, optional

        Returns
        -------
        tuple[MultiStateData]
        """
        if random_state is None:
            random_state = self.random_state
        y_samples = self.regressor.sample_y(x, n_samples=samples, random_state=random_state)
        return tuple(self.curve.convert_arrays(x, y_, np.full_like(y_, np.nan)) for y_ in y_samples)

    @staticmethod
    def optimizer(method='trust-constr'):
        def f(obj_func, initial_theta, bounds):
            constraints = [optimize.LinearConstraint(np.eye(initial_theta.shape[0]), bounds[:, 0], bounds[:, 1])]
            res = optimize.minimize(lambda theta: obj_func(theta=theta, eval_gradient=False),
                                    initial_theta,
                                    constraints=constraints,
                                    method=method,
                                    jac=lambda theta: obj_func(theta=theta, eval_gradient=True)[1],
                                    hess=optimize.BFGS(),
                                    options={'gtol': 1e-6})
            return res.x, res.fun

        return f

    @staticmethod
    def is_near_bounds(kernel, rtol=1e-4):
        params = kernel.get_params()
        bounds_sufix = '_bounds'
        bounds = (k for k in params if k.endswith(bounds_sufix) if str(params[k]) != 'fixed')
        for b in bounds:
            param = b[:-len(bounds_sufix)]
            value = params[param]
            lower_upper = params[b]
            if param == 'scale':
                value = _tri_matrix_to_flat(value)
                lower_upper = np.array([_tri_matrix_to_flat(m) for m in lower_upper])
            else:
                value = np.log(value)
                lower_upper = np.log(lower_upper)
            atol = (lower_upper[1] - lower_upper[0]) * rtol
            if np.any(np.abs(value - lower_upper) < atol):
                return True
        return False


if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from curves import OSCCurve
    from sklearn.gaussian_process import kernels

    sn_name = 'SDSS-II SN 10450'
    bands = "g',r',i'".split(',')

    k1 = kernels.RBF(length_scale_bounds=(1e-4, 1e4))
    k2 = kernels.RBF(length_scale_bounds=(1e-4, 1e4))
    # k2 = kernels.WhiteKernel()
    # k3 = kernels.ConstantKernel(constant_value_bounds='fixed')
    k3 = kernels.WhiteKernel()

    m = np.array([[1, 0, 0],
                  [0.5, 1, 0],
                  [0.5, 0.5, 1]])
    m_bounds = (np.array([[1e-4, 0, 0],
                          [-1e2, -1e3, 0],
                          [-1e2, -1e2, -1e3]]),
                np.array([[1e4, 0, 0],
                          [1e2, 1e3, 0],
                          [1e2, 1e2, 1e3]]))

    colors = {"g'": 'g', "r'": 'r', "i'": 'brown'}

    curve = OSCCurve.from_name(sn_name, bands=bands).binned(bin_width=1, discrete_time=True).filtered(sort='filtered')
    x_ = np.linspace(curve.X[:,1].min(), curve.X[:,1].max(), 101)
    interpolator = GPInterpolator(
        curve, (k1, k2, k3), m, m_bounds,
        optimize_method=None,  #'trust-constr',
        n_restarts_optimizer=0,
        random_state=0
    )
    msd = interpolator(x_)
    for i, band in enumerate(bands):
        plt.subplot(2, 2, i+1)
        blc = curve[band]
        plt.errorbar(blc['x'], blc['y'], blc['err'], marker='x', ls='', color=colors[band])
        plt.plot(msd.odict[band].x, msd.odict[band].y, color=colors[band], label=band)
        plt.grid()
        plt.legend()
    plt.show()
