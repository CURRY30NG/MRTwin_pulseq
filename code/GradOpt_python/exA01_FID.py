"""
Created on Tue Jan 29 14:38:26 2019
@author: mzaiss

"""
experiment_id = 'ex01_FID'
sequence_class = "gre_dream"
experiment_description = """
FID or 1 D imaging / spectroscopy
"""
excersize = """
A01.1. alter flipangle flips[3,0,0], find flip angle for max signal, guess function signal(flip_angle) ~= ...
A01.2. set flip to 90, alter number of spins
A01.3. alter phase and adc rot
A01.4. alter event_time
A01.5. fit signal, alter R2star
A01.6. alter dB0
"""
#%%
#matplotlib.pyplot.close(fig=None)
#%%
import os, sys
import numpy as np
import scipy
import scipy.io
from  scipy import ndimage
import torch
import cv2
import matplotlib.pyplot as plt
from torch import optim
import core.spins
import core.scanner
import core.nnreco
import core.opt_helper
import core.target_seq_holder
import core.FID_normscan
import warnings
import matplotlib.cbook
warnings.filterwarnings("ignore",category=matplotlib.cbook.mplDeprecation)


from importlib import reload
reload(core.scanner)

double_precision = False
do_scanner_query = False

use_gpu = 1
gpu_dev = 0

if sys.platform != 'linux':
    use_gpu = 0
    gpu_dev = 0
print(experiment_id)    
print('use_gpu = ' +str(use_gpu)) 

# NRMSE error function
def e(gt,x):
    return 100*np.linalg.norm((gt-x).ravel())/np.linalg.norm(gt.ravel())
    
# torch to numpy
def tonumpy(x):
    return x.detach().cpu().numpy()

# get magnitude image
def magimg(x):
  return np.sqrt(np.sum(np.abs(x)**2,2))

def magimg_torch(x):
  return torch.sqrt(torch.sum(torch.abs(x)**2,1))

def tomag_torch(x):
    return torch.sqrt(torch.sum(torch.abs(x)**2,-1))

# device setter
def setdevice(x):
    if double_precision:
        x = x.double()
    else:
        x = x.float()
    if use_gpu:
        x = x.cuda(gpu_dev)    
    return x 

#############################################################################
## S0: define image and simulation settings::: #####################################
sz = np.array([4,4])                      # image size
extraMeas = 1                               # number of measurmenets/ separate scans
NRep = extraMeas*sz[1]                      # number of total repetitions
NRep = 1                                   # number of total repetitions
szread=256
T = szread + 7                               # number of events F/R/P
NSpins = 26**2                               # number of spin sims in each voxel
NCoils = 1                                  # number of receive coil elements
noise_std = 0*1e-3                          # additive Gaussian noise std
kill_transverse = False                     #
import time; today_datestr = time.strftime('%y%m%d')
NVox = sz[0]*sz[1]

#############################################################################
## S1: Init spin system and phantom::: #####################################
# initialize scanned object
spins = core.spins.SpinSystem(sz,NVox,NSpins,use_gpu+gpu_dev,double_precision=double_precision)

cutoff = 1e-12
#real_phantom = scipy.io.loadmat('../data/phantom2D.mat')['phantom_2D']
#real_phantom = scipy.io.loadmat('../data/numerical_brain_cropped.mat')['cropped_brain']
real_phantom = np.zeros((128,128,5), dtype=np.float32); real_phantom[64:80,64:80,:2]=1; real_phantom[64:80,64:80,2]=0.1; real_phantom[64:80,64:80,3]=0; real_phantom[64:80,64:80,4]=1;

real_phantom_resized = np.zeros((sz[0],sz[1],5), dtype=np.float32)
for i in range(5):
    t = cv2.resize(real_phantom[:,:,i], dsize=(sz[0],sz[1]), interpolation=cv2.INTER_CUBIC)
    if i == 0:
        t[t < 0] = 0
    elif i == 1 or i == 2:
        t[t < cutoff] = cutoff        
    real_phantom_resized[:,:,i] = t
    
real_phantom_resized[:,:,1] *= 1 # Tweak T1
real_phantom_resized[:,:,2] *= 100000 # Tweak T2
real_phantom_resized[:,:,3] += 0 # Tweak dB0
real_phantom_resized[:,:,4] *= 1 # Tweak rB1

spins.set_system(real_phantom_resized)

plt.figure("""phantom""")
param=['PD','T1','T2','dB0','rB1']
for i in range(5):
    plt.subplot(151+i), plt.title(param[i])
    ax=plt.imshow(real_phantom_resized[:,:,i], interpolation='none')
    fig = plt.gcf()
    fig.colorbar(ax) 
fig.set_size_inches(18, 3)
plt.show()
   
#begin nspins with R*
R2star = 250.0
omega = np.linspace(0,1,NSpins) - 0.5   # cutoff might bee needed for opt.
omega = np.expand_dims(omega[:],1).repeat(NVox, axis=1)
omega*=0.99 # cutoff large freqs
omega = R2star * np.tan ( np.pi  * omega)
spins.omega = torch.from_numpy(omega.reshape([NSpins,NVox])).float()
spins.omega = setdevice(spins.omega)
## end of S1: Init spin system and phantom ::: #####################################


#############################################################################
## S2: Init scanner system ::: #####################################
scanner = core.scanner.Scanner_fast(sz,NVox,NSpins,NRep,T,NCoils,noise_std,use_gpu+gpu_dev,double_precision=double_precision)

B1plus = torch.zeros((scanner.NCoils,1,scanner.NVox,1,1), dtype=torch.float32)
B1plus[:,0,:,0,0] = torch.from_numpy(real_phantom_resized[:,:,4].reshape([scanner.NCoils, scanner.NVox]))
B1plus[B1plus == 0] = 1    # set b1+ to one, where we dont have phantom measurements
B1plus[:] = 1
scanner.B1plus = setdevice(B1plus)

#############################################################################
## S3: MR sequence definition ::: #####################################
# begin sequence definition
# allow for extra events (pulses, relaxation and spoiling) in the first five and last two events (after last readout event)
adc_mask = torch.from_numpy(np.ones((T,1))).float()
adc_mask[:5]  = 0
adc_mask[-2:] = 0
scanner.set_adc_mask(adc_mask=setdevice(adc_mask))

# RF events: flips and phases
flips = torch.zeros((T,NRep,2), dtype=torch.float32)
flips[3,0,0] = 90*np.pi/180  # GRE/FID specific, GRE preparation part 1 : 90 degree excitation 
flips = setdevice(flips)  
scanner.set_flip_tensor_withB1plus(flips)

# rotate ADC according to excitation phase
rfsign = ((flips[3,:,0]) < 0).float()
scanner.set_ADC_rot_tensor(-flips[3,:,1] + np.pi/2 + np.pi*rfsign) #GRE/FID specific

# event timing vector 
event_time = torch.from_numpy(0.08*1e-3*np.ones((scanner.T,scanner.NRep))).float()
event_time = setdevice(event_time)

# gradient-driver precession
# Cartesian encoding
grad_moms = torch.zeros((T,NRep,2), dtype=torch.float32)
grad_moms = setdevice(grad_moms)

scanner.init_gradient_tensor_holder()
scanner.set_gradient_precession_tensor(grad_moms,sequence_class)  # refocusing=False for GRE/FID, adjust for higher echoes
## end S3: MR sequence definition ::: #####################################


#############################################################################
## S4: MR simulation forward process ::: #####################################
scanner.init_signal()
scanner.forward(spins, event_time)
  
fig=plt.figure("""signals""")
ax1=plt.subplot(131)
ax=plt.plot(tonumpy(scanner.signal[0,:,:,0,0]).transpose().ravel(),label=str(NSpins))
#plt.plot(tonumpy(scanner.signal[0,:,:,1,0]).transpose().ravel(),label=str(NSpins))
plt.title('signal')
plt.legend()
plt.ion()

fig.set_size_inches(64, 7)
plt.show()
                        
#%%
t=np.cumsum(tonumpy(event_time).ravel())
y=tonumpy(scanner.signal[0,:,:,0,0]).transpose().ravel()
t=t[6:-2]; y=y[6:-2]
def fit_func(t, a, R,c):
    return a*np.exp(-R*t) + c   

p=scipy.optimize.curve_fit(fit_func,t,y,p0=(np.mean(y), 1,np.min(y)))
print(p[0][1])

fig=plt.figure("""fit""")
ax1=plt.subplot(131)
ax=plt.plot(t,y,label='data')
plt.plot(t,fit_func(t,p[0][0],p[0][1],p[0][2]),label="f={:.2}*exp(-{:.2}*t)+{:.2}".format(p[0][0], p[0][1],p[0][2]))
plt.title('fit')
plt.legend()
plt.ion()

fig.set_size_inches(64, 7)
plt.show()
            