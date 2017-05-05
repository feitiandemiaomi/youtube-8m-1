CUDA_VISIBLE_DEVICES=0 python train.py \
        --train_dir="../model/video_chain_moe16_hidden_combine/" \
        --frame_features=False \
        --model=HiddenCombineChainModel \
        --label_loss=MultiTaskCrossEntropyLoss \
        --feature_names="mean_rgb,mean_audio" \
        --feature_sizes="1024,128" \
        --train_data_pattern="/Youtube-8M/data/video/train/train*" \
        --batch_size=1024 \
        --multitask=True \
        --support_type="label,label,label,label" \
        --moe_num_mixtures=8 \
        --keep_checkpoint_every_n_hours=0.25 \
        --num_readers=4 \
        --base_learning_rate=0.01

