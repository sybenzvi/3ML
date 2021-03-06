import collections
import os
import sys

import matplotlib.pyplot as plt
import numpy
from astromodels import Parameter
from cthreeML.pyModelInterface import pyToCppModelInterface
from hawc import liff_3ML
from matplotlib import gridspec

from threeML.io.file_utils import file_existing_and_readable, sanitize_filename
from threeML.plugin_prototype import PluginPrototype

defaultMinChannel = 0
defaultMaxChannel = 9

__instrument_name = "HAWC"


class HAWCLike(PluginPrototype):
    def __init__(self, name, maptree, response, n_transits=None, **kwargs):

        # This controls if the likeHAWC class should load the entire
        # map or just a small disc around a source (faster).
        # Default is the latter, which is way faster. LIFF will decide
        # autonomously which ROI to use depending on the source model

        self.fullsky = False

        if 'fullsky' in kwargs.keys():
            self.fullsky = bool(kwargs['fullsky'])

        self.name = str(name)

        # Sanitize files in input (expand variables and so on)

        self.maptree = os.path.abspath(sanitize_filename(maptree))

        self.response = os.path.abspath(sanitize_filename(response))

        # Check that they exists and can be read

        if not file_existing_and_readable(self.maptree):
            raise IOError("MapTree %s does not exist or is not readable" % maptree)

        if not file_existing_and_readable(self.response):
            raise IOError("Response %s does not exist or is not readable" % response)

        # Post-pone the creation of the LIFF instance to when
        # we have the likelihood model

        self.instanced = False

        # Number of transits
        if n_transits is not None:

            self._n_transits = float(n_transits)

        else:

            self._n_transits = None

            # Default value for minChannel and maxChannel

        self.minChannel = int(defaultMinChannel)
        self.maxChannel = int(defaultMaxChannel)

        # By default the fit of the CommonNorm is deactivated

        self.deactivateCommonNorm()

        # This is to keep track of whether the user defined a ROI or not

        self.roi_ra = None

        # Further setup

        self.__setup()

    def setROI(self, ra, dec, radius, fixedROI=False):

        self.roi_ra = ra
        self.roi_dec = dec

        self.roi_radius = radius

        self.fixedROI = fixedROI

    def __setup(self):

        # I put this here so I can use it both from the __init__ both from
        # the __setstate__ methods

        # Create the dictionary of nuisance parameters

        self.nuisanceParameters = collections.OrderedDict()
        self.nuisanceParameters['CommonNorm'] = Parameter("CommonNorm", 1.0, min_value=0.5, max_value=1.5,
                                                          delta=0.01)
        self.nuisanceParameters['CommonNorm'].fix = True

    def __getstate__(self):

        # This method is used by pickle before attempting to pickle the class

        # Return only the objects needed to recreate the class
        # IN particular, we do NOT return the theLikeHAWC class,
        # which is not pickeable. It will instead be recreated
        # on the other side

        d = {}

        d['name'] = self.name
        d['maptree'] = self.maptree
        d['response'] = self.response
        d['model'] = self.model
        d['n_transits'] = self._n_transits
        d['minChannel'] = self.minChannel
        d['maxChannel'] = self.maxChannel

        d['roi_ra'] = self.roi_ra

        if self.roi_ra is not None:
            d['roi_dec'] = self.roi_dec
            d['roi_radius'] = self.roi_radius

        return d

    def __setstate__(self, state):

        # This is used by pickle to recreate the class on the remote
        # side
        name = state['name']
        maptree = state['maptree']
        response = state['response']
        ntransits = state['ntransits']

        self._n_transits = ntransits

        # Now report the class to its state

        self.__init__(name, maptree, response)

        if state['roi_ra'] is not None:
            self.setROI(state['roi_ra'], state['roi_dec'], state['roi_radius'], state['fixedROI'])

        self.set_active_measurements(state['minChannel'], state['maxChannel'])

        self.set_model(state['model'])

    def set_active_measurements(self, minChannel, maxChannel):

        self.minChannel = int(minChannel)
        self.maxChannel = int(maxChannel)

        if self.instanced:
            sys.stderr.write("Since the plugins was already used before, the change in active measurements" +
                             "will not be effective until you create a new JointLikelihood or Bayesian" +
                             "instance")

    def set_model(self, likelihood_model_instance):
        '''
        Set the model to be used in the joint minimization. Must be a LikelihoodModel instance.
        '''

        # Instance the python - C++ bridge

        self.model = likelihood_model_instance

        self.pymodel = pyToCppModelInterface(self.model)

        # Now init the HAWC LIFF software

        try:

            # Load all sky
            # (ROI will be defined later)

            if self._n_transits is None:

                self.theLikeHAWC = liff_3ML.LikeHAWC(self.maptree,
                                                     self.response,
                                                     self.pymodel,
                                                     self.minChannel,
                                                     self.maxChannel,
                                                     self.fullsky)

            else:

                self.theLikeHAWC = liff_3ML.LikeHAWC(self.maptree,
                                                     self._n_transits,
                                                     self.response,
                                                     self.pymodel,
                                                     self.minChannel,
                                                     self.maxChannel,
                                                     self.fullsky)



            if self.roi_ra is None and self.fullsky:
                raise RuntimeError("You have to define a ROI with the setROI method")

            if self.roi_ra is not None and self.fullsky:
                self.theLikeHAWC.SetROI(self.roi_ra, self.roi_dec, self.roi_radius, self.fixedROI)

        except:

            print("Could not instance the LikeHAWC class from LIFF. " +
                  "Check that HAWC software is working")

            raise

        else:

            self.instanced = True

        # Now set a callback in the CommonNorm parameter, so that if the user or the fit
        # engine or the Bayesian sampler change the CommonNorm value, the change will be
        # propagated to the LikeHAWC instance

        self.nuisanceParameters['CommonNorm'].add_callback(self._CommonNormCallback)

    def _CommonNormCallback(self, value):

        self.theLikeHAWC.SetCommonNorm(value)

    def get_name(self):
        '''
        Return a name for this dataset (likely set during the constructor)
        '''
        return self.name

    def activateCommonNorm(self):

        self.fitCommonNorm = True

    def deactivateCommonNorm(self):

        self.fitCommonNorm = False

    def get_log_like(self):

        '''
        Return the value of the log-likelihood with the current values for the
        parameters
        '''

        self.pymodel.update()

        logL = self.theLikeHAWC.getLogLike(self.fitCommonNorm)

        return logL

    def calcTS(self):

        '''
        Return the value of the log-likelihood test statistic, defined as
        2*[log(LL_model) - log(LL_bkg)]
        '''

        self.pymodel.update()

        TS = self.theLikeHAWC.calcTS(self.fitCommonNorm)

        return TS

    def get_nuisance_parameters(self):
        '''
        Return a list of nuisance parameters. Return an empty list if there
        are no nuisance parameters
        '''

        return self.nuisanceParameters.keys()

    def inner_fit(self):

        self.theLikeHAWC.SetBackgroundNormFree(self.fitCommonNorm)

        self.pymodel.update()

        logL = self.theLikeHAWC.getLogLike(self.fitCommonNorm)

        self.nuisanceParameters['CommonNorm'].value = self.theLikeHAWC.CommonNorm()

        return logL

    def display(self, radius=0.5):

        figs = []

        nsrc = self.model.get_number_of_point_sources()

        for srcid in range(nsrc):
            ra, dec = self.model.get_point_source_position(srcid)

            model = numpy.array(self.theLikeHAWC.GetTopHatExpectedExcesses(ra, dec, radius))

            signal = numpy.array(self.theLikeHAWC.GetTopHatExcesses(ra, dec, radius))

            bkg = numpy.array(self.theLikeHAWC.GetTopHatBackgrounds(ra, dec, radius))

            total = signal + bkg

            fig = plt.figure()

            gs = gridspec.GridSpec(2, 1, height_ratios=[2, 1])
            gs.update(hspace=0)

            sub = plt.subplot(gs[0])

            nHitBins = numpy.arange(self.minChannel, self.maxChannel + 1)

            sub.errorbar(nHitBins, total, yerr=numpy.sqrt(total),
                         capsize=0, color='black', label='Observation',
                         fmt='.')

            sub.plot(nHitBins, model + bkg, label='Model + bkg')

            plt.legend(bbox_to_anchor=(1.05, 1), loc=2, numpoints=1)

            # Residuals

            sub1 = plt.subplot(gs[1])

            # Using model variance to account for low statistic

            resid = (signal - model) / model

            sub1.axhline(0, linestyle='--')

            sub1.errorbar(nHitBins, resid,
                          yerr=numpy.sqrt(total) / model,
                          capsize=0, fmt='.')

            sub.set_xlim([nHitBins.min() - 0.5, nHitBins.max() + 0.5])

            sub.set_yscale("log", nonposy='clip')

            sub.set_ylabel("Counts per bin")

            # sub1.set_xscale("log")

            sub1.set_xlabel("Analysis bin")

            sub1.set_ylabel(r"$\frac{excess - mod.}{mod.}$", fontsize=20)

            sub1.set_xlim([nHitBins.min() - 0.5, nHitBins.max() + 0.5])

            sub.set_xticks([])
            sub1.set_xticks(nHitBins)

            figs.append(fig)

        return figs

    def writeModelMap(self, fileName):

        self.theLikeHAWC.WriteModelMap(fileName)

    def writeResidualMap(self, fileName):

        self.theLikeHAWC.WriteResidualMap(fileName)
