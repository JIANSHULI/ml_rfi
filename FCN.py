import numpy as np
import tensorflow as tf
import h5py
import matplotlib
matplotlib.use("AGG")
import matplotlib.pyplot as plt
#from pyuvdata import UVData
from xrfi import xrfi_simple
tf.logging.set_verbosity(tf.logging.INFO)
from time import time

def import_test_data(filename,bl_tup=(9,89),rescale=1.0):
    uvd = UVData()
    uvd.read_miriad(filename)
    a1,a2 = bl_tup
    data = np.nan_to_num(np.copy(uvd.get_data(a1,a2)))
    data*=rescale
    return data

def fold(data,ch_fold=16,labels=False):
    # We want to fold over in frequency
    # this will be done for both waterfalls and labels
    # data should be in (times,freqs) format
    ntimes,nfreqs = np.shape(data)
    dfreqs = int(nfreqs/ch_fold)
    if labels:
        data_fold = np.zeros((ntimes,nfreqs)).reshape(ch_fold,ntimes,dfreqs)
    else:
        data_fold = np.zeros((ntimes,nfreqs,2)).reshape(ch_fold,ntimes,dfreqs,2)
    for i in range(ch_fold):
        if labels:
            data_fold[i,:,:] = data[:,i*dfreqs:(i+1)*dfreqs]
        else:
            hold = np.nan_to_num(np.log10(np.abs(data[:,i*dfreqs:(i+1)*dfreqs]+np.random.rand(ntimes,dfreqs)))).real
            data_fold[i,:,:,0] = (hold - np.nanmean(hold))/np.nanmax(np.abs(hold)) #theres a better way to do this
            data_fold[i,:,:,1] = np.angle(data[:,i*dfreqs:(i+1)*dfreqs])
    return data_fold.real

def transpose(X):
    return X.T

def normalize(X):
    sh = np.shape(X)
    LOGabsX = np.nan_to_num(np.log10(np.abs(X+(1e-7)*np.random.rand(sh[0],sh[1])))).real
    return (LOGabsX-np.nanmean(LOGabsX))/np.nanmax(np.abs(LOGabsX))

def foldl(data,ch_fold=16):
    sh = np.shape(data)
    _data = data.T.reshape(ch_fold,sh[1]/ch_fold,-1)
    _DATA = np.array(map(transpose,_data))
    return _DATA

def fold2(data,ch_fold=16):
    sh = np.shape(data)
    _data = data.T.reshape(ch_fold,sh[1]/ch_fold,-1)
    _DATA = np.array(map(transpose,_data))
    DATA = np.stack((np.array(map(normalize,_DATA)),np.angle(_DATA)),axis=-1)
    return DATA

def unfold(data_fold,nchans):
    ch_fold,ntimes,dfreqs = np.shape(data_fold)
    data = np.zeros_like(data_fold).reshape(60,1024)
    for i in range(ch_fold):
        data[:,i*dfreqs:(i+1)*dfreqs] = data_fold[i,:,:]
    return data

def unfold2(data_fold,nchans=1024):
    ch_fold,ntimes,dfreqs = np.shape(data_fold)
    data_ = np.array(map(transpose,data_fold))
    _data = data_.reshape(ch_fold*dfreqs,ntimes).T
    return _data
    
def stacked_layer(input_layer,num_filter_layers,kt,kf,activation,stride,pool,bnorm=True):
    """
    Creates a 3x stacked layer of convolutional layers ###
    """
    conva = tf.layers.conv2d(inputs=input_layer,
                             filters=num_filter_layers,
                             kernel_size=[kt,kf],
                             padding="same",
                             activation=activation)
    
    convb = tf.layers.conv2d(inputs=conva,
                             filters=num_filter_layers,
                             kernel_size=[kt,kf],
                             padding="same",
                             activation=activation)

    convc = tf.layers.conv2d(inputs=convb,
                             filters=num_filter_layers,
                             kernel_size=[kt,kf],
                             padding="same",
                             activation=activation)
    if bnorm:
    	bnorm_conv = tf.contrib.layers.batch_norm(convc,scale=True)
    else:
    	bnorm_conv = convc

    pool = tf.layers.max_pooling2d(inputs=bnorm_conv,
                                    pool_size=pool,
                                    strides=stride)
    return pool

def t_layer(input_layer,num_filter_layers,kt,kf,activation):
    conv = tf.layers.conv2d(inputs=input_layer,
                                 filters=num_filter_layers,
                                 kernel_size=[kt,kf],
                                 padding="same",
                                 activation=activation)
    return conv
        
def upsample(input_layer,out_size):
    """
    Creates an upsampling layer which passes an input layer through two fully connected 
    layers and then into a convolutional layer that expands the filter dimension for
    reshaping into an upsampled output
    """
    sh = input_layer.get_shape().as_list()
    #print sh,out_size
    f_layers = int((1.*out_size[0]*out_size[1]*out_size[2])/(1.*sh[2]*sh[1]))
    #print 'f_kayers: ',f_layers
    layer_reshape = tf.reshape(input_layer, [-1,sh[1]*sh[2]*sh[3]])
    fc_layer_reshape = tf.reshape(layer_reshape, [-1,sh[1],sh[2],sh[3]])
    upsamp = tf.layers.conv2d(inputs=fc_layer_reshape,
                             filters=f_layers,
                             kernel_size=[3,3],
                             padding="same",
                             activation=tf.nn.relu)    
    upsamp_reshape = tf.reshape(upsamp, [-1,out_size[0],out_size[1],out_size[2]])
    return tf.contrib.layers.batch_norm(upsamp_reshape,scale=True)

def dense(input_layer, out_size):
    """                                                                                                                                              
    Combines 4 fully connected layers for a dense output after the conv. stacked                                                                     
    and upsampling layers                                                                                                                            
    """
    sh = input_layer.get_shape().as_list()
    try:
        scale = out_size[0]*out_size[1]
    except:
        scale = out_size[0]
    #print 'scale: ',scale
    try:
        input_layer_reshape = tf.reshape(input_layer, [-1,sh[1]*sh[2]])
    except:
        input_layer_reshape = tf.reshape(input_layer, [-1,sh[1]])

    fc3 = tf.layers.dense(input_layer_reshape, units=scale, activation=tf.nn.elu)
    try:
        fc3_reshape = tf.reshape(fc3, [-1,out_size[0],out_size[1]])
    except:
        fc3_reshape = tf.reshape(fc3, [-1,out_size[0]])
    return fc3_reshape

def cnn(features,labels,mode):
    """
    Model for CNN

    features: Visibility Array (batch#, time, freq, channel)
    labels: RFI flag array (batch#, time, freq)
    mode: used by tensorflow to distinguish training, evaluation, and testing
    """ 

    activation=tf.nn.relu # rectified exponential linear activation unit
    # kernel size
    kt = 3 #
    kf = 3 #

    # 4D tensor: batch size, height (ntimes), width (nfreq), channels (1)
    input_layer = tf.reshape(features["x"],[-1,60,64,2]) # this can be made size indep.

    # 3x stacked layers similar to VGG
    #in: 60,64,2
    slayer1 = stacked_layer(input_layer,64,kt,kf,activation,[2,2],[2,2],bnorm=True)

    #1: 30,32,64
    slayer2 = stacked_layer(slayer1,128,kt,kf,activation,[2,2],[2,2],bnorm=True)

    #2: 15,16,128
    slayer3 = stacked_layer(slayer2,4*192,kt,kf,activation,[3,2],[3,2],bnorm=True) 

    #3: 5,8,768
    slayer4 = stacked_layer(slayer3,4*384,kt,kf,activation,[1,1],[1,1],bnorm=True)    

    #4 6,32,1536
    slayer5 = stacked_layer(slayer4,1920,1,1,activation,[1,1],[1,1],bnorm=True)

    #5 5,16,1920
    # Transpose convolution (deconvolve)

    upsamp = tf.layers.conv2d_transpose(slayer5,filters=2,kernel_size=[56,57])
#    upsamp = tf.layers.conv2d(inputs=slayer5,
#                              filters=192,
#                              kernel_size=[1,1],
#                              padding="same",
#                              activation=tf.nn.relu)
    print 'Transpose Conv Shape: ',np.shape(upsamp)
    final_conv = tf.reshape(upsamp,[-1,60*64,2])

    # Grab some output weight info for tensorboard
#    tf.summary.image('FullyConnected_stacked_layer5',tf.reshape(final_conv[0,:,:], [-1,60,64,2]))

    predictions = {
        "classes": tf.argmax(input=final_conv, axis=2),
        "probabilities": tf.nn.softmax(final_conv,name="softmax_tensor")
    }

    try:    
        loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=final_conv)
    except:
        pass

    if mode == tf.estimator.ModeKeys.TRAIN:
        print 'Mode is train.'
        optimizer = tf.train.GradientDescentOptimizer(learning_rate=.001)
        train_op = optimizer.minimize(loss=loss,global_step=tf.train.get_global_step())
        return tf.estimator.EstimatorSpec(mode=mode,loss=loss,train_op=train_op)

    if mode == tf.estimator.ModeKeys.PREDICT:
        print 'Mode is predict.'
        return tf.estimator.EstimatorSpec(mode=mode,predictions=predictions)

    eval_metric_ops = {
        "accuracy": tf.metrics.accuracy(labels=labels,predictions=predictions['classes'])        
}
    return tf.estimator.EstimatorSpec(mode=mode,loss=loss,eval_metric_ops=eval_metric_ops)

def main(args):
    # load data
    f1 = h5py.File('RealVisRFI_v3.h5','r') # Load in a real dataset 
    f2 = h5py.File('SimVisRFI_v2.h5','r') # Load in simulated data

    train = True
    evaluate = True
    test = True

    # We want to augment our training dataset with the entirety of the simulated data
    # but with only half of the real data. The remaining real data half will become
    # the evaluation dataset

    
    f1_r = 900#np.shape(f1['data'])[0]
    f2_s = np.shape(f2['data'])[0]

    print 'Size of real dataset: ',f1_r
    print ''
    # Cut up real dataset and labels
    if train|evaluate:
        f1_r = 900 #np.shape(f1['data'])[0] # Everything after 900 doesn't look good
        samples = range(f1_r)
        rnd_ind = np.random.randint(0,f1_r)
    else:
        rnd_ind = np.random.randint(0,f1_r)
        samples = [rnd_ind]
    time0 = time()
    f_real = np.array(map(fold2,f1['data'][:f1_r,:,:])).reshape(-1,60,64,2)
    f_real_labels = np.array(map(foldl,f1['flag'][:f1_r,:,:])).reshape(-1,60,64)
    print 'Training dataset loaded.'
    
    #for i in samples:        
    #    print i
    #    if i == samples[0]:
    #        f_real = fold2(f1['data'][i,:,:],16)
    #        f_real_labels = fold2(f1['flag'][i,:,:],16,labels=True)
    #    else:
    #        f_real = np.vstack((f_real,fold2(f1['data'][i,:,:],16)))
    #        f_real_labels = np.vstack((f_real_labels,fold2(f1['flag'][i,:,:],16,labels=True)))

    # Cut up sim dataset and labels
    if train:
        f_sim = np.array(map(fold2,f2['data'][:f2_s,:,:])).reshape(-1,60,64,2)
        f_sim_labels = np.array(map(foldl,f2['flag'][:f2_s,:,:])).reshape(-1,60,64)
        print 'Simulated training dataset loaded.'
#        for i in range(f2_s):
#            print i
#            if i ==0:
#                f_sim = fold2(f2['data'][i,:,:],16)
#                f_sim_labels = fold2(f2['flag'][i,:,:],16,labels=True)
#            else:
#                f_sim = np.vstack((f_sim,fold2(f2['data'][i,:,:],16)))
#                f_sim_labels = np.vstack((f_sim_labels,fold2(f2['flag'][i,:,:],16,labels=True)))

    real_sh = np.shape(f_real)

    # Format evaluation dataset
    if evaluate:
        eval_data = np.asarray(f_real[real_sh[0]/2:,:,:,:],dtype=np.float32)
        eval_labels = np.asarray(f_real_labels[real_sh[0]/2:,:,:],dtype=np.int32).reshape(-1,real_sh[1]*real_sh[2])

    # Format training dataset
    if train|evaluate:
        train_data = np.asarray(f_sim,dtype=np.float32)#np.asarray(np.vstack((f_sim,f_real[:real_sh[0]/2,:,:,:])),dtype=np.float32)
        train_labels = np.asarray(f_sim_labels,dtype=np.int32).reshape(-1,real_sh[1]*real_sh[2])#np.asarray(np.vstack((f_sim_labels,f_real_labels[:real_sh[0]/2,:,:])),dtype=np.int32).reshape(-1,real_sh[1]*real_sh[2])

        train0 = np.shape(train_data)[0]
        eval1 = np.shape(eval_data)[0]
        steps = train0

    # Format a single test dataset
    if test:
        test_data = np.asarray(fold2(f1['data'][rnd_ind,:,:],16), dtype=np.float32) # Random real visibility for testing
        test_labels = np.asarray(foldl(f1['flag'][rnd_ind,:,:],16), dtype=np.int32).reshape(-1,real_sh[1]*real_sh[2])

    # create Estimator
    rfiCNN = tf.estimator.Estimator(model_fn=cnn,model_dir='./checkpoint_Patch4_SimTrain/')

    if train:
        train_input_fn = tf.estimator.inputs.numpy_input_fn(
            x={"x":train_data},
            y=train_labels,
            batch_size=16,
            num_epochs=1000,
            shuffle=True,
        )

    if evaluate:
        eval_input_fn = tf.estimator.inputs.numpy_input_fn(
            x={"x":eval_data},
            y=eval_labels,
            num_epochs=100,
            shuffle=False
        )	

    if test:
        test_input_fn = tf.estimator.inputs.numpy_input_fn(
            x={"x":test_data}, ##### like 900 waterfalls!!!
            shuffle=False
        )
    
    if train:
        rfiCNN.train(input_fn=train_input_fn, steps=steps)

    if evaluate:
        eval_results = rfiCNN.evaluate(input_fn=eval_input_fn)
        try:
            output = open('eval_results.txt','w')
            output.write(eval_results)
        except:
            print 'No eval results saved.'
        print(eval_results)

    if test:
        rfiPredict = rfiCNN.predict(input_fn=test_input_fn)

    # Predict on the test dataset where labels are hidden
    print 'Prediction dataset is size: ',np.shape(train_data)[0]
    obs_flags = np.zeros((16,60,64))#np.shape(train_data)[0],60,64))
    for i,predicts in enumerate(rfiPredict):
        print i#np.shape(i),np.shape(predicts['probabilities'])
        obs_flags[i,:,:] = predicts['classes'].reshape(60,64)
#        if i == 0:
#            cnn_flags = predicts['classes'].reshape(1,60,64)
#        else:
#            cnn_flags = np.vstack((cnn_flags,predicts['classes'].reshape(1,60,64)))

    obs_flags = obs_flags.reshape(16,60,64)
    cnn_flags = unfold2(obs_flags)
    print time() - time0
    print 'Shape of CNN flags: ',np.shape(cnn_flags)
    print 'Shape of Test flags: ',np.shape(test_labels)
    #cnn_flags = unfold2(cnn_flags,1024)
    test_labels = unfold2(test_labels.reshape(-1,60,64),1024)
    plt.subplot(411)
    plt.imshow(cnn_flags,aspect='auto')
    plt.title('Predicted Flags')
    plt.colorbar()

    plt.subplot(412)
    plt.imshow(test_labels.reshape(-1,1024),aspect='auto')
    plt.title('XRFI Flags')
    plt.colorbar()

    plt.subplot(413)
    plt.imshow(np.log10(np.abs(f1['data'][rnd_ind,:,:])),aspect='auto')
    plt.colorbar()
    plt.title('Vis. Log Normalized Amp.')

    plt.subplot(414)
    plt.imshow(np.angle(f1['data'][rnd_ind,:,:]),aspect='auto')
    plt.colorbar()
    plt.title('Vis. Phs.')
        
    plt.savefig('RealData.png')
        
    cnn_flags = np.logical_not(cnn_flags)
    xrfi_flags = np.logical_not(test_labels.reshape(-1,1024))
    plt.subplot(211)
    plt.imshow(np.log10(np.abs(f1['data'][rnd_ind,:,:]*cnn_flags)),aspect='auto')
    plt.title('Predicted Flags Applied')
        
    plt.subplot(212)
    plt.imshow(np.log10(np.abs(f1['data'][rnd_ind,:,:]*xrfi_flags)),aspect='auto')
    plt.title('XRFI Flags Applied')

    plt.savefig('VisApplied.png')

if __name__ == "__main__":
    tf.app.run()
