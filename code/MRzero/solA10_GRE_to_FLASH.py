"""
Created on Tue Jan 29 14:38:26 2019
@author: mzaiss

"""
experiment_id = 'solA10_GRE_to FLASH'
sequence_class = "gre_dream"
experiment_description = """
2 D imaging
"""
excercise = """
The current sequence has a very long scan time.
A10.1. calculate the total scan time of the sequence
A10.1. lower the recovery time after each repetition to event_time[-1,:] =  0.1 . 
    What do you observe?
    Whats the shortest time to still have a good image?   
A10.2. lower the flip angle to 5 degree.
    What do you observe?
    whats now the shortest time to still have a good image? (This is also called "Long TR spoiling")
A10.3. Turn off all phase gradients and look at the signals. 
    Do you see additional echoes? Where are they originating from? 
    Try to find a way to get rid of transverse magnetization from the previous rep using the rf phase (RF spoiling)
A10.4. find a way to get rid of transverse magnetization from the previous rep using a gradient. (gradient spoiling, spoiler or crusher gradient)
    can you now go even shorter with the event times? how short?
A10.5.  combine both gradient and RF spoiling
A10.6.  Include a phase backblip to balance all phase gradients

Now you have generated the famous FLASH sequence.

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

def phaseimg(x):
    return np.angle(1j*x[:,:,1]+x[:,:,0])

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
sz = np.array([32,32])                      # image size
extraMeas = 1                               # number of measurmenets/ separate scans
NRep = extraMeas*sz[1]                      # number of total repetitions
szread=sz[1]
T = szread + 5 + 2                               # number of events F/R/P
NSpins = 16**2                               # number of spin sims in each voxel
NCoils = 1                                  # number of receive coil elements
noise_std = 0*100*1e-3                        # additive Gaussian noise std
kill_transverse = False                     #
import time; today_datestr = time.strftime('%y%m%d')
NVox = sz[0]*szread

do_voxel_rand_ramp_distr = True

#############################################################################
## S1: Init spin system and phantom::: #####################################
# initialize scanned object
spins = core.spins.SpinSystem(sz,NVox,NSpins,use_gpu+gpu_dev,double_precision=double_precision)

cutoff = 1e-12
#real_phantom = scipy.io.loadmat('../../data/phantom2D.mat')['phantom_2D']
real_phantom = scipy.io.loadmat('../../data/numerical_brain_cropped.mat')['cropped_brain']

real_phantom_resized = np.zeros((sz[0],sz[1],5), dtype=np.float32)
for i in range(5):
    t = cv2.resize(real_phantom[:,:,i], dsize=(sz[0],sz[1]), interpolation=cv2.INTER_CUBIC)
    if i == 0:
        t[t < 0] = 0
    elif i == 1 or i == 2:
        t[t < cutoff] = cutoff        
    real_phantom_resized[:,:,i] = t
    

    
real_phantom_resized[:,:,1] *= 1 # Tweak T1
real_phantom_resized[:,:,2] *= 1 # Tweak T2
real_phantom_resized[:,:,3] *= 0 # Tweak dB0
real_phantom_resized[:,:,4] *= 1 # Tweak rB1

spins.set_system(real_phantom_resized)

if 0:
    plt.figure("""phantom""")
    param=['PD','T1','T2','dB0','rB1']
    for i in range(5):
        plt.subplot(151+i), plt.title(param[i])
        ax=plt.imshow(real_phantom_resized[:,:,i], interpolation='none')
        fig = plt.gcf()
        fig.colorbar(ax) 
    fig.set_size_inches(18, 3)
    plt.show()
   
#begin nspins with R2* = 1/T2*
R2star = 0.0
omega = np.linspace(0,1,NSpins) - 0.5   # cutoff might bee needed for opt.
omega = np.expand_dims(omega[:],1).repeat(NVox, axis=1)
omega*=0.99 # cutoff large freqs
omega = R2star * np.tan ( np.pi  * omega)
spins.omega = torch.from_numpy(omega.reshape([NSpins,NVox])).float()
spins.omega = setdevice(spins.omega)
## end of S1: Init spin system and phantom ::: #####################################


#############################################################################
## S2: Init scanner system ::: #####################################
scanner = core.scanner.Scanner(sz,NVox,NSpins,NRep,T,NCoils,noise_std,use_gpu+gpu_dev,double_precision=double_precision)

if do_voxel_rand_ramp_distr:
    dim = scanner.setdevice(torch.sqrt(torch.tensor(scanner.NSpins).float()))
    off = 1 / dim
    mg = 1000
    intravoxel_dephasing_ramp = setdevice(torch.zeros((scanner.NSpins,scanner.NVox,2), dtype=torch.float32))
    xvb, yvb = torch.meshgrid([torch.linspace(-1+off,1-off,dim.int()), torch.linspace(-1+off,1-off,dim.int())])
    
    for i in range(scanner.NVox):
        # this generates an anti-symmetric distribution in x
        Rx1= torch.randn(torch.Size([dim.int()/2,dim.int()]))*off
        Rx2=-torch.flip(Rx1, [0])
        Rx= torch.cat((Rx1, Rx2),0)
        # this generates an anti-symmetric distribution in y
        Ry1= torch.randn(torch.Size([dim.int(),dim.int()/2]))*off
        Ry2=-torch.flip(Ry1, [1])
        Ry= torch.cat((Ry1, Ry2),1)
                    
        xv = xvb + Rx
        yv = yvb + Ry
    
        intravoxel_dephasing_ramp[:,i,:] = np.pi*torch.stack((xv.flatten(),yv.flatten()),1)
    
    # remove coupling w.r.t. R2
    permvec = np.random.choice(scanner.NSpins,scanner.NSpins,replace=False)
    intravoxel_dephasing_ramp = intravoxel_dephasing_ramp[permvec,:,:]
    
    intravoxel_dephasing_ramp /= setdevice(torch.from_numpy(scanner.sz).float().unsqueeze(0).unsqueeze(0))
    scanner.intravoxel_dephasing_ramp = intravoxel_dephasing_ramp

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
flips[3,:,0] = 5*np.pi/180  # 90deg excitation now for every rep
flips[3,:,1]=torch.arange(0,50*NRep,50)*np.pi/180 

def get_phase_cycler(n, dphi,flag=0):
    out = np.cumsum(np.arange(n) * dphi)  #  from Alex (standard)
    if flag:
        for j in range(0,n,1):               # from Zur et al (1991)
            out[j] = dphi/2*(j**2 +j+2)
    out = torch.from_numpy(np.mod(out, 360).astype(np.float32))
    return out    

flips[3,:,1]=get_phase_cycler(NRep,117)*np.pi/180 


flips = setdevice(flips)
scanner.init_flip_tensor_holder()
scanner.set_flip_tensor_withB1plus(flips)
# rotate ADC according to excitation phase
rfsign = ((flips[3,:,0]) < 0).float()
scanner.set_ADC_rot_tensor(-flips[3,:,1] + np.pi/2 + np.pi*rfsign) #GRE/FID specific

# event timing vector 
event_time = torch.from_numpy(0.08*1e-3*np.ones((scanner.T,scanner.NRep))).float()
event_time[-1,:] =  0.01
event_time = setdevice(event_time)

# gradient-driver precession
# Cartesian encoding
grad_moms = torch.zeros((T,NRep,2), dtype=torch.float32)
grad_moms[4,:,1] = -0.5*szread
#grad_moms[4,:,1] = -2.0*szread
grad_moms[5:-2,:,1] = 1.0
grad_moms[4,:,0] = torch.arange(0,NRep,1)-NRep/2 #phase blib
grad_moms[-2,:,0] = -grad_moms[4,:,0]  # phase backblip
#grad_moms[-2,:,1] = 1.5*szread         # spoiler (even numbers sometimes give stripes, best is ~ 1.5 kspaces, for some reason 0.2 works well,too  )
grad_moms[-2,:,1] = 2.0* sz[0]
#grad_moms[-2,:,1] = 0.2* sz[0]
grad_moms = setdevice(grad_moms)

scanner.init_gradient_tensor_holder(do_voxel_rand_ramp_distr=do_voxel_rand_ramp_distr)
scanner.set_gradient_precession_tensor(grad_moms,sequence_class)  # refocusing=False for GRE/FID, adjust for higher echoes
## end S3: MR sequence definition ::: #####################################



#############################################################################
## S4: MR simulation forward process ::: #####################################
scanner.init_signal()
#scanner.forward_fast(spins, event_time)
scanner.forward(spins, event_time)

fig=plt.figure("""seq and image"""); fig.set_size_inches(60, 9); 
plt.subplot(411); plt.ylabel('RF, time, ADC'); plt.title("Total acquisition time ={:.2} s".format(tonumpy(torch.sum(event_time))))
plt.plot(np.tile(tonumpy(adc_mask),NRep).flatten('F'),'.',label='ADC')
plt.plot(tonumpy(event_time).flatten('F'),'.',label='time')
plt.plot(tonumpy(flips[:,:,0]).flatten('F'),label='RF')
major_ticks = np.arange(0, T*NRep, T) # this adds ticks at the correct position szread
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.subplot(412); plt.ylabel('gradients')
plt.plot(tonumpy(grad_moms[:,:,0]).flatten('F'),label='gx')
plt.plot(tonumpy(grad_moms[:,:,1]).flatten('F'),label='gy')
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.subplot(413); plt.ylabel('signal')
plt.plot(tonumpy(scanner.signal[0,:,:,0,0]).flatten('F'),label='real')
plt.plot(tonumpy(scanner.signal[0,:,:,1,0]).flatten('F'),label='imag')
ax=plt.gca(); ax.set_xticks(major_ticks); ax.grid()
plt.legend()
plt.show()
  
#%% ############################################################################
## S5: MR reconstruction of signal ::: #####################################

spectrum = tonumpy(scanner.signal[0,adc_mask.flatten()!=0,:,:2,0].clone()) 
spectrum = spectrum[:,:,0]+spectrum[:,:,1]*1j # get all ADC signals as complex numpy array
kspace=spectrum
spectrum = np.roll(spectrum,szread//2,axis=0)
spectrum = np.roll(spectrum,NRep//2,axis=1)
space = np.zeros_like(spectrum)
space = np.fft.ifft2(spectrum)
space = np.roll(space,szread//2-1,axis=0)
space = np.roll(space,NRep//2-1,axis=1)
space = np.flip(space,(0,1))
       
fig=plt.figure("""blabla""");
fig.set_size_inches(30, 19); 

plt.subplot(4,6,19)
plt.imshow(real_phantom_resized[:,:,0], interpolation='none'); plt.xlabel('PD')
plt.subplot(4,6,20)
plt.imshow(real_phantom_resized[:,:,3], interpolation='none'); plt.xlabel('dB0')

plt.subplot(4,6,21)
plt.imshow(np.log(np.abs(kspace)), interpolation='none'); plt.xlabel('kspace')
plt.subplot(4,6,19)
plt.imshow(np.abs(space), interpolation='none',aspect = sz[0]/szread); plt.xlabel('mag_img')
plt.subplot(4,6,20)
mask=(np.abs(space)>0.2*np.max(np.abs(space)))
plt.imshow(np.angle(space)*mask, interpolation='none',aspect = sz[0]/szread); plt.xlabel('phase_img')
plt.show()                     
