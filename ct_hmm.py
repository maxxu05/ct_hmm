import numpy as np
from scipy.stats import norm
from scipy.stats import multivariate_normal
from scipy.linalg import expm
from scipy.special import logsumexp
from scipy.interpolate import interp1d
from scipy.integrate import odeint
import itertools
import bisect

class CT_HMM_LEARNER:
    def __init__(self,Q,pi0,ls_mu,ls_sigma,patients = [], structure = "fc", method="eigen", bound = True):
        '''
        @param Q: rate matrix
        @param pi0: start state probabilities
        @param ls_mu: list of means
        @param ls_sigma: variances
        @param patients: list of Patient objects
        '''
        self.pi0 = pi0
        self.ls_mu = ls_mu
        if len(ls_mu) == 1:
            self.num_state = len(ls_mu[0])
        else: 
            self.num_state = len(ls_mu[0])
            for i in range(1,len(ls_mu)):
                self.num_state *= len(ls_mu[i])
        self.ls_sigma = ls_sigma
        
        self.Q_struct = self.create_Q_struct(struct=structure, numstates=self.num_state)
        print(self.Q_struct)
        if Q is None:
            np.random.seed(65)
            self.Q = np.random.rand(*self.Q_struct.shape)
            self.Q[self.Q_struct == 0] = 0
            for i in range(self.num_state):
                self.Q[i,i] = -np.sum(self.Q[i,:])
            self.Q_orig = np.copy(self.Q)
        else:
            self.Q = Q
            self.Q_orig = np.copy(Q)
            self.Q_orig = np.copy(self.Q)

        self.patients = patients
        self.method = method
        self.bound = bound
        
    def get_time_intervals(self):
        '''
        @summary: iteration over patients and collect all time intervals
        '''
        time_intervals = []
        for patient in self.patients:
            observation_times = patient.observation_times
            L = len(observation_times)
            for i in range(1,L):
                time_intervals.append(observation_times[i]-observation_times[i-1])
        self.time_intervals = np.unique(np.array(sorted(time_intervals)))
        
    def create_Q_struct(self, struct, numstates):
        Q_struct = np.zeros((numstates,numstates))
        if struct == "fc":
            for i in range(numstates):
                for j in range(numstates):
                    if i != j:
                        Q_struct[i,j] = 1
            
        elif struct == "forward_step":
            temp = []
            for i in range(len(self.ls_mu)):
                temp_2 = []
                for j in range(len(self.ls_mu[i])):
                    temp_2.append(j)
                temp.append(temp_2)

            for i, state_combos_i in enumerate(itertools.product(*temp)): #represents going down the rows in q matrix, the original state
                for j, state_combos_j in enumerate(itertools.product(*temp)):# this represents the state we are transitioning into
                    if state_combos_i != state_combos_j:
                        within_step = True
                        for idx in range(len(state_combos_j)):
                            if (state_combos_j[idx] - state_combos_i[idx] < 0) or (state_combos_j[idx] - state_combos_i[idx]) > 1:
                                # if it is not forward or if the difference between the two states is greater than a step of 1
                                within_step = False
                        if within_step:
                            Q_struct[i,j] = 1
                                                
        elif struct == "forward_any":
            temp = []
            for i in range(len(self.ls_mu)):
                temp_2 = []
                for j in range(len(self.ls_mu[i])):
                    temp_2.append(j)
                temp.append(temp_2)
            
            for i, state_combos_i in enumerate(itertools.product(*temp)): #represents going down the rows in q matrix, the original state
                for j, state_combos_j in enumerate(itertools.product(*temp)):# this represents the state we are transitioning into
                    if state_combos_i != state_combos_j:
                        forward = True
                        for idx in range(len(state_combos_j)):
                            if state_combos_j[idx] - state_combos_i[idx] < 0: # if it is not forward, then skip
                                forward = False
                                continue
                        if forward:
                            Q_struct[i,j] = 1
        else:
            raise Exception('Unknown method, must be fc, forward_step, or forward_any') # Don't! If you catch, likely to hide bugs.
        
        return Q_struct
    
    def matrix_exponentials(self):
        '''
        @summary: iterate over time intervals and calculate relevant matrix exponentials expm(Q*t) for current Q
        '''
        expm_Q_t = {}
        for t in self.time_intervals:
            expm_Q_t[t] = expm(self.Q*t)
        self.expm_Q_t = expm_Q_t
    
    def run_EM(self,fast_eigen=False,tol=1e-4,verbose=True,update_sigma=0, update_mu = True):
        '''
        @param fast_eigen: True if use fast_eigen, False if use grouping instead
        @param tol: the absolute difference between old and new log-likelihood needed to terminate
        @param verbose: if True print the log-likelihood at each iteration
        @param update_sigma: if 1 learn variances of observation model
        @param update_mu: if True learn means of observation model
        @description: run EM algorithm until convergence
        '''
        old_log_likelihood = -np.inf
        new_log_likelihood = 0
        while np.abs(old_log_likelihood-new_log_likelihood)>tol:
            self.EM_step(update_sigma=update_sigma, update_mu=update_mu, fast_eigen=fast_eigen)
            old_log_likelihood = new_log_likelihood
            new_log_likelihood = self.calculate_log_likelihood()
            if verbose:
                print('log-likelihood')
                print(new_log_likelihood)
    
    def calculate_log_likelihood(self):
        log_likelihood = 0
        for patient in self.patients:
            log_likelihood += np.sum(np.log(patient.C))
        return log_likelihood
    
    def calculate_log_likelihood_patient(self, patient):
        log_likelihood = 0
        log_likelihood += np.sum(np.log(patient.C))
        return log_likelihood
    
    def set_eigendecomposition(self):
        '''
        @summary: perform eigendecomposition of current Q matrix
        @set self.U: right eigenvectors
        @set self.V: inverse of right eigenvectors
        @set self.D: eigenvalues
        '''
        D, self.U = np.linalg.eig(self.Q)
        self.D = np.diag(D)
        self.V = np.linalg.inv(self.U)
    
    def EM_step(self, update_sigma=0, update_mu=True, fast_eigen = True):
        num_state = self.num_state
        Nij = 0
        TauI = 0
        #storage arrays
        mu_numerator = np.zeros(num_state)
        mu_denominator = np.zeros(num_state)
        sigma_numerator = np.zeros(num_state)
        sigma_denominator = np.zeros(num_state)
        pi0_numerator = np.zeros(num_state)
        pi0_denominator = np.zeros(num_state)
        #pre-comput matrix exponentials and eigendecomposition
        self.matrix_exponentials()
        self.set_eigendecomposition()
        if fast_eigen==False and self.method =="eigen":
            #pre-comput all end-state conditioned expectations
            self.Eigen_all_end_state_conditioned()
        for patient in self.patients:
            #get emissions for patient
            patient.get_all_emissions_gaussian(self)
            if self.method == "eigen":
                Nij += self.Eigen_Nij_all_times(patient,fast_eigen=fast_eigen)
                TauI += self.Eigen_TauI_all_times(patient)
            elif self.method == "expm":
                temptau, tempn = self.Expm_TauI_Nij_all_times(patient)
                Nij += tempn
                TauI += temptau
            
            #get Alpha forward recursion
            patient.alpha_forward_recursion(self)
            Alpha = patient.Alpha
            C = patient.C
            #get Beta backward recursion
            patient.beta_backward_recursion(self)
            Beta = patient.Beta
            #iterate over observations
            for t in range(patient.num_obs):
                #get the marginal beliefs
                gamma_t = Alpha[t,:]*Beta[t,:]
                if t==0:
                    pi0_numerator+=gamma_t
                    pi0_denominator+=np.sum(gamma_t)
#                 mu_numerator += gamma_t*patient.O[t]
#                 mu_denominator += gamma_t
#                 sigma_numerator += gamma_t*((np.repeat(patient.O[t],num_state)-self.ls_mu)**2)
#                 sigma_denominator += gamma_t
        self.update_model_params(Nij,TauI,mu_numerator,mu_denominator,pi0_numerator,pi0_denominator,sigma_numerator,sigma_denominator,update_sigma=update_sigma, update_mu=update_mu)
    
    def update_model_params(self,Nij,TauI,mu_numerator,mu_denominator,pi0_numerator,pi0_denominator,sigma_numerator,sigma_denominator,update_sigma=False, update_mu=True):
        #update Q matrix off-diagonal elements
        for i in range(self.num_state):
            for j in range(self.num_state):
                if self.Q_struct[i,j] == 1:
                    self.Q[i,j] = Nij[i,j]/TauI[i]
                    if np.isnan(self.Q[i,j]):
                        import pdb; pdb.set_trace()
                    if self.bound:
                        if self.Q[i,j] < 1e-10: # added bounding to prevent floating point errors that lead expm(Q) to introduce negative values
                            self.Q[i,j] = 1e-10

        #storage lists
        ls_mu = []
        ls_sigma = []
        #update model parameters
        for i in range(self.num_state):
            self.Q[i,i] = 0
            self.Q[i,i] = -np.sum(self.Q[i,:])
            if update_mu:
                ls_mu.append(mu_numerator[i]/mu_denominator[i])
            if update_sigma:
                ls_sigma.append(sigma_numerator[i]/sigma_denominator[i])
        self.pi0 = pi0_numerator/pi0_denominator
        if update_mu:
            self.ls_mu = ls_mu
        if update_sigma:
            self.ls_sigma = ls_sigma
        
    def Eigen_all_end_state_conditioned(self):
        self.TauI_end_state = {}
        self.Nij_end_state = {}
        for t in self.time_intervals:
            Psi_eigen = self.calculate_Psi_eigen(t)
            for i in range(self.num_state):
                self.Eigen_TauI_time_interval_end_state(t,Psi_eigen,i)
                for j in range(self.num_state):
                    self.Eigen_Nij_time_interval_end_state(t,Psi_eigen,i,j)
    
    def Eigen_Nij_time_interval_end_state(self,t,Psi_eigen,i,j):
        Aij = np.multiply(np.outer(self.V[:,i],self.U[j,:]), Psi_eigen)
        if i!=j:
            self.Nij_end_state[(t,i,j)]=self.Q[i,j]*np.dot(np.dot(self.U,Aij),self.V)
        else:
            self.Nij_end_state[(t,i,j)]=0
    
    def Eigen_Nij_time_interval(self,Zeta_matrix, t, Psi_eigen):
        '''
        @param t: time interval between observations
        @param Zeta_matrix: this has the pairwise beliefs
        @param Psi_eigen: a matrix needed for Eigen method
        '''
        #Get Q matrix and number of states
        num_state = self.num_state
        #set up transition count matrix for specific time interval
        Nij_mat = np.zeros((num_state,num_state))
        F = Zeta_matrix/self.expm_Q_t[t]
        B = np.dot(np.dot(np.transpose(self.U), F), np.transpose(self.V))
        for i in range(num_state):
            for j in range(num_state):
                Aij = np.multiply(np.outer(self.V[:,i],self.U[j,:]), Psi_eigen)
                temp = np.multiply(Aij,B)
                if i!=j:
                    Nij_mat[i,j]=self.Q[i,j]*np.sum(temp)
        return Nij_mat
    
    def Eigen_TauI_time_interval_end_state(self,t,Psi_eigen,i):
        Ai = np.multiply(np.outer(self.V[:,i],self.U[i,:]), Psi_eigen)
        if i!=j:
            self.TauI_end_state[(t,i)]=self.Q[i,j]*np.dot(np.dot(self.U,Ai),self.V)

    def Eigen_TauI_time_interval(self,Zeta_matrix, t, Psi_eigen):
        #Get Q matrix and number of states
        num_state = np.shape(self.Q)[0]
        TauI = np.zeros(num_state)
        F = Zeta_matrix/self.expm_Q_t[t]
        B = np.dot(np.dot(np.transpose(self.U), F), np.transpose(self.V))
        for i in range(num_state):
            Ai = np.multiply(np.outer(self.V[:,i],self.U[i,:]), Psi_eigen)
            temp = np.multiply(Ai,B)
            TauI[i] = np.sum(temp)
            
        return TauI

    def calculate_Psi_eigen(self,t):
        num_state = np.shape(self.Q)[0]
        D, U = np.linalg.eig(self.Q)
        Psi_eigen = np.zeros((num_state,num_state))
        for p in range(num_state):
            for q in range(num_state):
                if D[p]==D[q]:
                    Psi_eigen[p,q] = t*np.exp(t*D[p])
                else:
                    Psi_eigen[p,q] = (np.exp(t*D[p])-np.exp(t*D[q]))/(D[p]-D[q])
        return Psi_eigen
        
    def Expm_TauI_Nij_all_times(self, patient):
        T = patient.num_obs
        num_state = self.num_state
        patient.alpha_forward_recursion(self)
        Alpha = patient.Alpha
        patient.beta_backward_recursion(self)
        Beta = patient.Beta
        patient.get_all_emissions_gaussian(self)
        
        TauI = np.zeros(num_state)
        Nij_mat = np.zeros((num_state,num_state))
        
        A = np.zeros((num_state * 2, num_state * 2))
        A[0:num_state, 0:num_state] = self.Q
        A[(num_state):num_state*2+1, (num_state):num_state*2+1] = self.Q
        
        for i in range(num_state):
            A[i, i + num_state] = 1
            for t_index in range(1,T):
                t = patient.observation_times[t_index]-patient.observation_times[t_index-1]
                expm_A = expm(A * t)
                Pt_kl = self.expm_Q_t[t]
                Zeta_matrix = self.get_zeta(t,patient,Alpha[t_index-1],Beta[t_index],patient.O[t_index])
                
                temp_sum = 0.0
                for k in range(num_state):
                    for l in range(num_state):
                        if Pt_kl[k,l] != 0:
                            temp_sum = temp_sum + Zeta_matrix[k, l] * expm_A[k, l + num_state] / Pt_kl[k,l]
                        
                TauI[i] = TauI[i] + temp_sum

                ni = temp_sum * (-self.Q[i, i])
                Nij_mat[i, i] = Nij_mat[i, i] + ni
            A[i, i + num_state] = 0
            
            
        for i in range(num_state):
            for j in range(num_state):
                if self.Q_struct[i,j] == 1:
                    A[i, j + num_state] = 1
                    for t_index in range(1,T):
                        t = patient.observation_times[t_index]-patient.observation_times[t_index-1]
                        expm_A = expm(A * t)
                        Pt_kl = self.expm_Q_t[t]    # change later
                        Zeta_matrix = self.get_zeta(t,patient,Alpha[t_index-1],Beta[t_index],patient.O[t_index])
                        
                        temp_sum = 0.0
                        for k in range(num_state):
                            for l in range(num_state):
                                if Pt_kl[k,l] != 0:
                                    temp_sum = temp_sum + Zeta_matrix[k, l] * expm_A[k, l + num_state] / Pt_kl[k,l]

                        nij = temp_sum * (self.Q[i, j])
                        Nij_mat[i, j] = Nij_mat[i, j] + nij

                A[i, j + num_state] = 0
        
        return TauI, Nij_mat
        
    def Eigen_Nij_all_times(self,patient,fast_eigen=True):
        '''
        @param patient: a Patient object
        '''
        T = patient.num_obs
        num_state = self.num_state
        Nij_mat = np.zeros((num_state,num_state))
        patient.alpha_forward_recursion(self)
        Alpha = patient.Alpha
        patient.beta_backward_recursion(self)
        Beta = patient.Beta
        patient.get_all_emissions_gaussian(self)
        if fast_eigen:
            for i in range(1,T):
                t = patient.observation_times[i]-patient.observation_times[i-1]
                Zeta_matrix = self.get_zeta(t,patient,Alpha[i-1],Beta[i],patient.O[i])
                Psi_eigen = self.calculate_Psi_eigen(t)
                Nij_mat += self.Eigen_Nij_time_interval(Zeta_matrix, t, Psi_eigen)
        else:
            for l in range(1,T):
                t = patient.observation_times[l]-patient.observation_times[l-1]
                Zeta_matrix = self.get_zeta(t,patient,Alpha[l-1],Beta[l],patient.O[l])
                F = Zeta_matrix/self.expm_Q_t[t]
                for i in range(self.num_state):
                    for j in range(self.num_state):
                        Nij_mat[i,j] += np.sum(np.multiply(self.Nij_end_state[(t,i,j)],F))
        return Nij_mat

    def Eigen_TauI_all_times(self,patient,fast_eigen=True):
        T = patient.num_obs
        num_state = self.num_state
        TauI = np.zeros(num_state)
        patient.alpha_forward_recursion(self)
        Alpha = patient.Alpha
        patient.beta_backward_recursion(self)
        Beta = patient.Beta
        if fast_eigen:
            for i in range(1,T):
                t = patient.observation_times[i]-patient.observation_times[i-1]
                Zeta_matrix = self.get_zeta(t,patient,Alpha[i-1],Beta[i],patient.O[i])
                Psi_eigen = self.calculate_Psi_eigen(t)
                TauI += self.Eigen_TauI_time_interval(Zeta_matrix, t, Psi_eigen)
#                 if (np.isnan(TauI).any()):
#                     import pdb; pdb.set_trace()
        else:
            for l in range(1,T):
                t = patient.observation_times[l]-patient.observation_times[l-1]
                Zeta_matrix = self.get_zeta(t,patient,Alpha[l-1],Beta[l],patient.O[l])
                F = Zeta_matrix/self.expm_Q_t[t]
                for i in range(self.num_state):
                    TauI[i]+=np.sum(np.multiply(self.TauI_end_state[(t,i)],F))
#         if (np.isnan(TauI).any()):
#             import pdb; pdb.set_trace()
        return TauI
    
    def get_zeta(self,t,patient,alpha,beta,obs):
        '''
        @summary: calculate zeta from the paper
        @param i,j: state from and to
        @param t: time interval length
        @param alpha: alpha vector for time t
        @param beta: beta vector for time t+1
        @param globalParams: as everywhere
        @param observations_t: all observations for time t, all types
        @return: the zeta for state i to j
        '''
        b = patient.b_s(obs)
        likelihood = np.dot(alpha,np.dot(self.expm_Q_t[t],beta*b))
        return self.expm_Q_t[t]*np.outer(alpha, np.transpose(b*beta))/likelihood

class Patient:
    '''
    When setting this based on real data, we need to set the following:
    self.observation_times: the times of observations
    self.num_obs: how many observations we have
    self.O: the observation values
    '''
    def __init__(self,end_time=10,censored = False):
        self.end_time = end_time
        self.censored = False
        
    def get_all_emissions_gaussian(self,ct_hmm_learner):
        '''
        @param ct_hmm_learner: a CT_HMM_Learner object
        @description: iterates over all observations and gets the likelihood for each state
        @set self.emissions: a dictionary where keys are observation values and values are a list of likelihoods per state
        '''
        gaussian_emissions = {}
        for i in range(len(self.O)):
            gaussian_emissions[self.O[i]] = self. emission_Gaussian(ct_hmm_learner,self.O[i])
        self.emissions = gaussian_emissions
    
    def emission_Gaussian(self,ct_hmm_learner,obs):
        '''
        @param ct_hmm_learner: a CT_HMM_Learner object
        @param obs: an observation
        @description: for a single observation, get the likelihood under each state
        @return: emissions, a list of likelihoods (one for each state)
        '''
        ls_mu_all = ct_hmm_learner.ls_mu
        ls_sigma_all = ct_hmm_learner.ls_sigma
        emissions = []
        if len(ls_mu_all) == 1:
            ls_mu_combo = ls_mu_all[0]
            ls_sigma_combo = ls_sigma_all[0]
            for ls_mu, ls_sigma in zip(ls_mu_combo, ls_sigma_combo):
                emissions.append(norm.pdf(obs,ls_mu,ls_sigma))
        else:
            ls_mu_combo = list(itertools.product(*ls_mu_all))
            ls_sigma_combo = list(itertools.product(*ls_sigma_all))
            for ls_mu, ls_sigma in zip(ls_mu_combo, ls_sigma_combo):
                emissions.append(multivariate_normal.pdf(obs,ls_mu,[sigma ** 2 for sigma in ls_sigma]))
                
        return emissions
    
    def b_s(self,obs):
        '''
        @param obs: observation value
        @return: list of likelihoods for observation, one for each state
        '''
        return self.emissions[obs]
        
    def alpha_forward_recursion(self,ct_hmm_learner):
        '''
        @summary: perform the forward recursion, get alpha value for all time steps
        @param ct_hmm_learner: a CT_HMM_LEARNER object
        '''
        O = self.O
        T = self.num_obs
        #Initialize Alpha for observations
        Alpha = np.zeros((T,ct_hmm_learner.num_state))
        C = np.zeros(T)
        Alpha[0],C[0] = self.get_alpha_vector(0,O[0],ct_hmm_learner,0,pi0=ct_hmm_learner.pi0)
        #add a column of 0's to the observation times
        observation_times = np.zeros((len(self.observation_times),2))
        observation_times[:,0] = self.observation_times
        all_times = observation_times
        #iterate over all times, keep track of observation index and event index
        T = np.shape(all_times)[0]
        for i in range(1,T):
            #if i is 0 simply increment
            t = self.observation_times[i]-self.observation_times[i-1]
            Alpha[i,:],C[i] = self.get_alpha_vector(t,O[i],ct_hmm_learner,Alpha[i-1,:])
            #increment observation
        self.Alpha = Alpha
        self.C = C
        
    def get_alpha_vector(self,t,obs, ct_hmm_learner,alpha_prev,pi0=0):
        '''
        @summary: alpha vector for a single time step
        @params: same as for state
        @return: alpha vector
        '''
        #initialize alpha vector, one element per state
        alpha = np.zeros(ct_hmm_learner.num_state)
        if obs==None:
            b=1
        else:
            #get the emissions
            b = self.b_s(obs)
        if t!=0:
            #if this is not the first observation
            if t in ct_hmm_learner.expm_Q_t:
                expm_matrix = ct_hmm_learner.expm_Q_t[t]
            else:
                expm_matrix = expm(Q*t)
            alpha = b*np.dot(np.transpose(expm_matrix),alpha_prev)
        else:
            #if this is the first observation
            alpha = pi0*b
        #get scaling factors
        c = np.sum(alpha)
        #scale alpha
        alpha = alpha/c
        #return alpha and the scaling factor
        return alpha, c

    def beta_backward_recursion(self,ct_hmm_learner):
        '''
        @param ct_hmm_learner: CT_HMM_LEARNER object
        '''
        #get scaling factors
        C = self.C
        #get number of observations
        T = self.num_obs
        #get observation values
        O = self.O
        #initialize Beta matrix to 1's
        Beta = np.ones((T,ct_hmm_learner.num_state))
        observation_times = np.zeros((len(self.observation_times),2))
        observation_times[:,0] = self.observation_times
        all_times = observation_times
        T = len(all_times[:,0])
        #iterate backwards
        for i in range(T-2,-1,-1):
            t = self.observation_times[i+1]-self.observation_times[i]
            Beta[i,:] = self.get_beta_vector(C,t,i,O[i+1],ct_hmm_learner,Beta[i+1,:])
            if (np.isnan(Beta).any()):
                import pdb; pdb.set_trace()
        
        self.Beta = Beta
    
    def get_beta_vector(self,C,t,i,next_obs, ct_hmm_learner,beta_after):
        '''
        @summary: beta vector
        @params C: scaling factors
        @params t: time interval
        @param i: indexes observation number
        @param next_obs: next observations
        @param ct_hmm_learner: 
        '''
        #get emissions for the observation
        b = self.b_s(next_obs)
        if t in ct_hmm_learner.expm_Q_t:
            expm_matrix = ct_hmm_learner.expm_Q_t[t]
        else:
            expm_matrix = expm(Q*t)
        components = np.log(beta_after)+np.log(expm_matrix)+np.log(b)
        logSumExp = logsumexp(components,1)
        beta = np.exp(logSumExp)
        #scale beta
        beta = beta/C[i+1]
        if (np.isnan(beta).any()):
            import pdb; pdb.set_trace()
        return beta
    
    def predict(self, t,ct_hmm_learner,predict_observations=True):
        '''
        @params t: the time (from 0) that we want to predict
        @params ct_hmm_learner: the CT_HMM_LEARNER object
        @params predict_observations: if True, predict the observation at time t based on mean, else return state probability vector
        '''
        prev_time = 0
        for idx, obs_times in enumerate(self.observation_times):
            if obs_times < t:
                prev_time = idx
            
        alpha_predict = self.get_alpha_vector(t-self.observation_times[-1],None, ct_hmm_learner,self.Alpha[-1])[0]
        mu = np.array(ct_hmm_learner.ls_mu)
        print(alpha_predict)
        if predict_observations:
            return np.dot(mu,alpha_predict)
        else:
            return alpha_predict

    def viterbi_decoding(self, ct_hmm_learner, time_step = 1):
        '''
        @params ct_hmm_learner: the CT_HMM_LEARNER object
        @params time_step: calculate 
        @return state trajectory with time_steps from initial to final observation time
        '''
        curr_time = self.observation_times[0]
        enter_state_time = np.zeros(ct_hmm_learner.num_state)
        miu_vals = np.zeros(ct_hmm_learner.num_state)
        best_state_path = [[] for i in range(ct_hmm_learner.num_state)]

        gaussian_emissions = self.emission_Gaussian(ct_hmm_learner, self.O[0])
        for i, state in enumerate(best_state_path):
            enter_state_time[i] = self.observation_times[0]

            state_pi0 = ct_hmm_learner.pi0[i]   
            miu_vals[i] = state_pi0 * gaussian_emissions[i]

        times = np.arange(self.observation_times[0], self.observation_times[-1], 1).tolist()
        for i in range(1, len(self.observation_times)):
            bisect.insort(times, self.observation_times[i])

        while curr_time <= self.end_time:
            if curr_time in self.observation_times:
                idx = self.observation_times.index(curr_time)
                gaussian_emissions = self.emission_Gaussian(ct_hmm_learner, self.O[idx])
            else:
                gaussian_emissions = 1


            for state_t in range(ct_hmm_learner.num_state):
                temp_miu_t_vals = []
                for state_t_minus_1 in range(ct_hmm_learner.num_state):
                    q_t_minus_1_i = ct_hmm_learner.Q[state_t_minus_1, state_t_minus_1]

                    if state_t == state_t_minus_1:
                        transition_prob = (-q_t_minus_1_i) * np.exp(q_t_minus_1_i*(curr_time - enter_state_time[state_t_minus_1]))
                    else:
                        transition_prob = ct_hmm_learner.Q[state_t_minus_1, state_t] / q_t_minus_1_i
                    temp_miu_t_vals.append(gaussian_emissions[state_t] * transition_prob * miu_vals[state_t_minus_1])

                # now we choose which previous state best connects to the current state
                best_state_t_minus_1_for_state_t = np.argmax(temp_miu_t_vals)
                # update enter time for dwelling times if we changed states
                if best_state_t_minus_1_for_state_t != state_t:
                    enter_state_time[state_t] = curr_time
                miu_vals[state_t] = temp_miu_t_vals[best_state_t_minus_1_for_state_t]
                best_state_path[state_t].append(best_state_t_minus_1_for_state_t)

        return best_state_path, times

class SSAProb:
    def __init__(self,L, T, Starts, Time, MaxDom, HasSpecificEndState, Ends, Q_mat):
        '''
        @params L = vector of lambda values, one for each state of the chain
        @params T = matrix of transition probabilities
        @params Starts = Vector of possible start states that will be analyzed
        @params Time = duration of time difference
        @params MaxDom = optionally, largest number of state sequences that can dominate
                a sequence before it is discarded. Defaults to 0, hence only
                non-dominated sequences would be returned.
        @params HasSpecificEndState = Whether there is a specific end state being aimed for
        @params Ends = Whether it ends
        @params Q_mat = matrix of transition rates
        '''
        self.Pt = expm(Q_mat * Time)
        self.L = L
        self.T = T
        self.Starts = Starts
        self.TimeGrid = Time
        self.MaxDom = MaxDom
        self.HasSpecificEndState = HasSpecificEndState
        self.Ends = Ends

    def StateSequenceAnalyze():
        '''
        @return SSAProb = the SSA problem being solved
        @return TimeGrid = the numerical grid for evaluation of state sequence
                probabilities and likelihoods
        @return Seqs = an NxN cell array where N is the number of states of the chain,
                in which .Seqs{i,j} is the set of non-dominated state sequences from
                state i to state j. Each sequence is represented by a structure which
                itself contains two fields: .seq is the state sequence, and .p is its
                probability as a function of time.
        '''
        # How many states in the system?
        NStates = len(self.L)

        # Initialize Seqs "cell" array (legacy MATLAB cell type)
        Seqs = np.empty((NStates, NStates), dtype=object)
        for i in range(NStates):
            for j in range(NStates):
                Seqs[i,j] = []

        # Initialize information for start states and enqueue their possible extensions
        Queue = []
        for i in range(len(self.Starts)):
            Start = self.Starts[i]
            TempSeq = {}
            TempSeq["seq"] = Start
            TempSeq["p"] = exp(-self.L(Start) @ self.TimeGrid.T) #probability of sequence
            TempSeq["ndom"] = 0
            Seqs[Start, Start].append(TempSeq)
            for j in range(NStates):
                if self.T[Start, j] > 0:
                    Queue.append([Start, j])

        # Keep processing sequences, as long as the queue is not empty!
        while Queue:
            # Get next sequence and process it unless it's parent is gone, so then it should be gone
            Seq = Queue[0]
            Queue = Queue[1:]

            Parent = FindParent(Seqs, Seq)

            if Parent is not None:
                # Compute the sequence's probability curve and create a structure for it
                TempSeq = {}
                TempSeq["seq"] = Seq
                TempSeq["p"] = ComputeP(Seq,Parent) #probability of sequence
                TempSeq["ndom"] = 0

                Seqs, ItsAKeeper = UpdateSeqs(Seqs, TempSeq)

                # If it wasn't dominated (or not too much), add the possible single-step extensions to the queue.
                if ItsAKeeper:
                    for i in range(NStates):
                        if self.T[Seq[-1], i] > 0:
                            Queue.append([Seq, i])



    def FindParent(Seqs, Seq):
        Parent = None
        if len(Seq) >= 2:
            PSeq = Seq[0:-1]
            PStart = PSeq[0]
            PEnd = PSeq[-1]
            for i in range(len(Seqs[PStart, PEnd])):
                TempSeq = Seqs[PStart, PEnd][i]
                if len(TempSeq["seq"]) == len(PSeq):
                    if (TempSeq["seq"] == PSeq).all():
                        Parent = TempSeq
                        return Parent
        return Parent

    def ComputeP(Seq, Parent):
        '''
        Compute the time-dependent probability of a state sequence
        '''
        NextToLastState = Parent["seq"][-1]
        pfunc = lambda t: interp1d(self.TimeGrid, Parent["p"], t)
        A = self.L[Seq[-1]]
        B = self.L[Parent["seq"][-1]] * self.T[NextToLastState, Seq[-1]]
        RHS = lambda t,y: A*y + B*pfunc(t)

        P = odeint(RHS, 0, self.TimeGrid)

        return P[:, 0]

    def UpdateSeqs(InSeqs, NewSeq):
        # Start and End States
        Start = NewSeq["seq"][0]
        End = NewSeq["seq"][-1]

        # Establish Dominance Relationships
        DomOthers = [] # Whether NewSeq dominates already found sequences
        for i in range(len(InSeqs[Start, End])):
            TempDiff = InSeqs[Start, End][i]["p"][1:] - NewSeq["p"][1:]
            # If NewSeq is dominated...
            if (TempDiff > 0).all()
                NewSeq["ndom"] += 1

            if (TempDiff < 0).all()
            DomOthers.append(i)

        # If NewSeq dominated, or dominated by too many other sequences, we discard it, and we're done.
        if NewSeq["ndom"] > self.MaxDom:
            ItsAKeeper = False
            OutSeqs = InSeqs
        else:
            ItsAKeeper = True
            OutSeqs = InSeqs

            ToKill = []
            for Other in DomOthers:
                OutSeqs[Start, End][Other]["ndom"] += 1
                if OutSeqs[Start, End][Other]["ndom"] > self.MaxDom:
                    ToKill.append(Other)
            if len(ToKill) != 0:
                ToKeep = [np.arange(0, len(OutSeqs[Start, End])) not in ToKill]
                OutSeqs[Start, End] = OutSeqs[Start, End][ToKeep]

            OutSeqs[Start, End].append(NewSeq)

        return OutSeqs, ItsAKeeper