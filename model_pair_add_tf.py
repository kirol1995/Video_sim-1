import tensorflow as tf
from tensorflow.python.keras.models import Model
from transformers import TFBertModel, create_optimizer
from model_transformer import Video_transformer, Transformer_Encoder


class NeXtVLAD(tf.keras.layers.Layer):
    def __init__(self, feature_size, cluster_size, output_size=1024, expansion=2, groups=8, dropout=0.2):
        super().__init__()
        self.feature_size = feature_size
        self.cluster_size = cluster_size
        self.expansion = expansion
        self.groups = groups

        self.new_feature_size = expansion * feature_size // groups
        self.expand_dense = tf.keras.layers.Dense(self.expansion * self.feature_size)
        # for group attention
        self.attention_dense = tf.keras.layers.Dense(self.groups, activation=tf.nn.sigmoid)
        self.activation_bn = tf.keras.layers.BatchNormalization() # MODIFY HERE

        # for cluster weights
        self.cluster_dense1 = tf.keras.layers.Dense(self.groups * self.cluster_size, activation=None, use_bias=False)
        # self.cluster_dense2 = tf.keras.layers.Dense(self.cluster_size, activation=None, use_bias=False)
        self.dropout = tf.keras.layers.Dropout(rate=dropout, seed=1)
        self.fc = tf.keras.layers.Dense(output_size, activation=None)

    def build(self, input_shape):
        self.cluster_weights2 = self.add_weight(name="cluster_weights2",
                                                shape=(1, self.new_feature_size, self.cluster_size),
                                                initializer=tf.keras.initializers.glorot_normal, trainable=True)
        self.built = True

    def call(self, inputs, **kwargs):
        image_embeddings, mask = inputs
        _, num_segments, _ = image_embeddings.shape
        if mask is not None:  # in case num of images is less than num_segments
            images_mask = tf.sequence_mask(mask, maxlen=num_segments)
            images_mask = tf.cast(tf.expand_dims(images_mask, -1), tf.float32)
            image_embeddings = tf.multiply(image_embeddings, images_mask)
        inputs = self.expand_dense(image_embeddings)
        attention = self.attention_dense(inputs)

        attention = tf.reshape(attention, [-1, num_segments * self.groups, 1])
        reshaped_input = tf.reshape(inputs, [-1, self.expansion * self.feature_size])

        activation = self.cluster_dense1(reshaped_input)
        activation = self.activation_bn(activation) # MODIFY HERE
        activation = tf.reshape(activation, [-1, num_segments * self.groups, self.cluster_size])
        activation = tf.nn.softmax(activation, axis=-1)  # shape: batch_size * (max_frame*groups) * cluster_size
        activation = tf.multiply(activation, attention)  # shape: batch_size * (max_frame*groups) * cluster_size

        a_sum = tf.reduce_sum(activation, -2, keepdims=True)  # shape: batch_size * 1 * cluster_size
        a = tf.multiply(a_sum, self.cluster_weights2)  # shape: batch_size * new_feature_size * cluster_size
        activation = tf.transpose(activation, perm=[0, 2, 1])  # shape: batch_size * cluster_size * (max_frame*groups)

        reshaped_input = tf.reshape(inputs, [-1, num_segments * self.groups, self.new_feature_size])

        vlad = tf.matmul(activation, reshaped_input)  # shape: batch_size * cluster_size * new_feature_size
        vlad = tf.transpose(vlad, perm=[0, 2, 1])  # shape: batch_size * new_feature_size * cluster_size
        vlad = tf.subtract(vlad, a)
        vlad = tf.nn.l2_normalize(vlad, 1)
        vlad = tf.reshape(vlad, [-1, self.cluster_size * self.new_feature_size])

        vlad = self.dropout(vlad)
        vlad = self.fc(vlad)
        return vlad


class SENet(tf.keras.layers.Layer):
    def __init__(self, channels, ratio=8, **kwargs):
        super(SENet, self).__init__(**kwargs)
        self.fc = tf.keras.Sequential([
            tf.keras.layers.Dense(channels // ratio, activation='relu', kernel_initializer='he_normal', use_bias=False),
            tf.keras.layers.Dense(channels, activation='sigmoid', kernel_initializer='he_normal', use_bias=False)
        ])

    def call(self, inputs, **kwargs):
        se = self.fc(inputs)
        outputs = tf.math.multiply(inputs, se)
        return outputs


class ConcatDenseSE(tf.keras.layers.Layer):
    """Fusion using Concate + Dense + SENet"""

    def __init__(self, hidden_size, se_ratio, **kwargs):
        super().__init__(**kwargs)
        self.fusion = tf.keras.layers.Dense(hidden_size, activation='relu')
        self.fusion_dropout = tf.keras.layers.Dropout(0.2)
        self.enhance = SENet(channels=hidden_size, ratio=se_ratio)

    def call(self, inputs, **kwargs):
        embeddings = tf.concat(inputs, axis=1)
        embeddings = self.fusion_dropout(embeddings)
        embedding = self.fusion(embeddings)
        embedding = self.enhance(embedding)

        return embedding


class MultiModal(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel.from_pretrained(config.bert_dir)
        self.bert_map = tf.keras.layers.Dense(1024, activation ='relu')
        self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
                                 output_size=config.vlad_hidden_size, dropout=config.dropout)
        self.video_tf = Video_transformer(num_hidden_layers=1, output_size=config.frame_embedding_size, 
                                        seq_len=config.max_frames, dropout=config.dropout)
        self.fusion_vis = ConcatDenseSE(config.vlad_hidden_size, config.se_ratio)
        self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')

        self.bert_optimizer_1, self.bert_lr_1 = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer_1, self.lr_1 = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables_1, self.num_bert_1, self.normal_variables_1, self.all_variables_1 = None, None, None, None

    def call(self, inputs, **kwargs):
        bert_embedding_1 = self.bert([inputs['input_ids_1'], inputs['mask_1']])[1]
        bert_embedding_1 = self.bert_map(bert_embedding_1)
        frame_num_1 = tf.reshape(inputs['num_frames_1'], [-1])

        video_tf_embedding_1, _ = self.video_tf([inputs['frames_1'], frame_num_1]) # b,32,1536
        video_tf_embedding_1 = tf.reduce_max(video_tf_embedding_1, 1)

        vision_embedding_1 = self.nextvlad([inputs['frames_1'], frame_num_1])
        vision_embedding_1 = vision_embedding_1 * tf.cast(tf.expand_dims(frame_num_1, -1) > 0, tf.float32)
        visual_emb_1 = self.fusion_vis([vision_embedding_1, video_tf_embedding_1])
        final_embedding_1 = self.fusion([visual_emb_1, bert_embedding_1])
        predictions_1 = self.classifier(final_embedding_1)

        bert_embedding_2 = self.bert([inputs['input_ids_2'], inputs['mask_2']])[1]
        bert_embedding_2 = self.bert_map(bert_embedding_2)
        frame_num_2 = tf.reshape(inputs['num_frames_2'], [-1])

        video_tf_embedding_2, _ = self.video_tf([inputs['frames_2'], frame_num_2]) # b,32,1536
        video_tf_embedding_2 = tf.reduce_max(video_tf_embedding_2, 1)

        vision_embedding_2 = self.nextvlad([inputs['frames_2'], frame_num_2])
        vision_embedding_2 = vision_embedding_2 * tf.cast(tf.expand_dims(frame_num_2, -1) > 0, tf.float32)
        visual_emb_2 = self.fusion_vis([vision_embedding_2, video_tf_embedding_2])
        final_embedding_2 = self.fusion([visual_emb_2, bert_embedding_2])
        predictions_2 = self.classifier(final_embedding_2)

        # vision_embedding = tf.concat([vision_embedding_1, vision_embedding_2], 0)
        # bert_embedding = tf.concat([bert_embedding_1, bert_embedding_2], 0)
        # predictions_2 = self.classifier(final_embedding_2)
        return final_embedding_1, final_embedding_2, predictions_1, predictions_2

    def get_variables(self):
        if not self.all_variables_1:  # is None, not initialized
            self.bert_variables_1 = self.bert.trainable_variables
            self.num_bert_1 = len(self.bert_variables_1)
            self.normal_variables_1 = self.nextvlad.trainable_variables + self.fusion.trainable_variables + \
                                    self.classifier.trainable_variables + self.bert_map.trainable_variables + \
                                    self.video_tf.trainable_variables + self.fusion_vis.trainable_variables
            self.all_variables_1 = self.bert_variables_1 + self.normal_variables_1
        return self.all_variables_1

    def optimize(self, gradients):
        bert_gradients_1 = gradients[:self.num_bert_1]
        self.bert_optimizer_1.apply_gradients(zip(bert_gradients_1, self.bert_variables_1))
        normal_gradients_1 = gradients[self.num_bert_1:]
        self.optimizer_1.apply_gradients(zip(normal_gradients_1, self.normal_variables_1))


class MultiModalV2(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel.from_pretrained(config.bert_dir)
        #self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
        #                         output_size=config.vlad_hidden_size, dropout=config.dropout)
        #self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels

        # reduce the vision embedding dims
        self.vision_transformer = Transformer_Encoder(num_layers=1,
                                                      d_model=config.frame_embedding_size,
                                                      num_heads=8,
                                                      dff=config.frame_embedding_size*2,
                                                      seq_len=config.max_frames)  # (bs, max_frames, 1024)
        self.frame_feat_fc = tf.keras.layers.Dense(config.hidden_size)
        self.frame_feat_fc_last = tf.keras.Sequential([
            tf.keras.layers.Dense(1, activation='relu'),
            tf.keras.layers.BatchNormalization(),
        ])

        # reduce the text embedding dims
        self.text_feat_fc = tf.keras.Sequential([
            tf.keras.layers.Dense(config.hidden_size, activation='relu'),
            tf.keras.layers.BatchNormalization(),
        ])
        self.text_feat_fc_last = tf.keras.Sequential([
            tf.keras.layers.Dense(1, activation='relu'),
            tf.keras.layers.BatchNormalization(),
        ])

        # transformer's seq_len is totally length
        self.transformer = Transformer_Encoder(num_layers=1,
                                               d_model=config.hidden_size,
                                               num_heads=4,
                                               dff=config.hidden_size * 4,
                                               seq_len=config.max_frames + config.bert_seq_length)  # (bs, total_seq_len, d_model)

        self.multi_feat_fc = tf.keras.layers.Dense(1, activation='relu')

        self.dropout = tf.keras.layers.Dropout(config.dropout)

        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')

        self.bert_optimizer_1, self.bert_lr_1 = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer_1, self.lr_1 = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables_1, self.num_bert_1, self.normal_variables_1, self.all_variables_1 = None, None, None, None

    def call(self, inputs, training, **kwargs):
    #     bert_embedding_1 = self.bert([inputs['input_ids_1'], inputs['mask_1']])[1]
    #     bert_embedding_1 = self.bert_map(bert_embedding_1)
    #     frame_num_1 = tf.reshape(inputs['num_frames_1'], [-1])

    #     video_tf_embedding_1, _ = self.video_tf([inputs['frames_1'], frame_num_1]) # b,32,1536
    #     video_tf_embedding_1 = tf.reduce_max(video_tf_embedding_1, 1)

    #     vision_embedding_1 = self.nextvlad([inputs['frames_1'], frame_num_1])
    #     vision_embedding_1 = vision_embedding_1 * tf.cast(tf.expand_dims(frame_num_1, -1) > 0, tf.float32)
    #     visual_emb_1 = self.fusion_vis([vision_embedding_1, video_tf_embedding_1])
    #     final_embedding_1 = self.fusion([visual_emb_1, bert_embedding_1])
    #     predictions_1 = self.classifier(final_embedding_1)

    #     bert_embedding_2 = self.bert([inputs['input_ids_2'], inputs['mask_2']])[1]
    #     bert_embedding_2 = self.bert_map(bert_embedding_2)
    #     frame_num_2 = tf.reshape(inputs['num_frames_2'], [-1])

    #     video_tf_embedding_2, _ = self.video_tf([inputs['frames_2'], frame_num_2]) # b,32,1536
    #     video_tf_embedding_2 = tf.reduce_max(video_tf_embedding_2, 1)

    #     vision_embedding_2 = self.nextvlad([inputs['frames_2'], frame_num_2])
    #     vision_embedding_2 = vision_embedding_2 * tf.cast(tf.expand_dims(frame_num_2, -1) > 0, tf.float32)
    #     visual_emb_2 = self.fusion_vis([vision_embedding_2, video_tf_embedding_2])
    #     final_embedding_2 = self.fusion([visual_emb_2, bert_embedding_2])
    #     predictions_2 = self.classifier(final_embedding_2)

        bert_embedding_1 = self.bert([inputs['input_ids_1'], inputs['mask_1']], training)[0] # 0: last_hidden_state, 1: poller_output
        bert_embedding_1 = self.text_feat_fc(bert_embedding_1)  # [bs, bert_seq_length, feature_dims]

        frame_num_1 = tf.reshape(inputs['num_frames_1'], [-1]) # frame num
        vision_embedding_1 = inputs['frames_1'] # [bs, max_frames, 1536]
        vision_embedding_1 = self.vision_transformer(vision_embedding_1) # [bs, max_frames, 1536]
        vision_embedding_1 = self.frame_feat_fc(vision_embedding_1)  # [bs, max_frames, hidden_size]

        embedding_1 = tf.concat([vision_embedding_1, bert_embedding_1], axis=1)
        embedding_1 = self.transformer(embedding_1)  # (bs, total_seq_len, d_model)
        embedding_1 = tf.transpose(embedding_1, perm=[0, 2, 1])  # (bs, d_model, total_seq_len)
        embedding_1 = self.multi_feat_fc(embedding_1)
        embedding_1 = tf.squeeze(embedding_1) # [bs, d_model]

        vision_embedding_1 = tf.transpose(vision_embedding_1, perm=[0, 2, 1])  # [bs, hidden_size, max_frames]
        vision_embedding_1 = self.frame_feat_fc_last(vision_embedding_1)  # [bs, hidden_size, 1]
        vision_embedding_1 = tf.squeeze(vision_embedding_1)

        bert_embedding_1 = tf.transpose(bert_embedding_1, perm=[0, 2, 1])  # [bs, hidden_size, bert_seq_length]
        bert_embedding_1 = self.text_feat_fc_last(bert_embedding_1)  # [bs, hidden_size, 1]
        bert_embedding_1 = tf.squeeze(bert_embedding_1)

        predictions_1 = self.dropout(embedding_1, training=training)
        predictions_1 = self.classifier(predictions_1)

        # 2 
        bert_embedding_2 = self.bert([inputs['input_ids_2'], inputs['mask_2']], training)[0] # 0: last_hidden_state, 1: poller_output
        bert_embedding_2 = self.text_feat_fc(bert_embedding_2)  # [bs, bert_seq_length, feature_dims]

        frame_num_2 = tf.reshape(inputs['num_frames_2'], [-1]) # frame num
        vision_embedding_2 = inputs['frames_2'] # [bs, max_frames, 1536]
        vision_embedding_2 = self.vision_transformer(vision_embedding_2) # [bs, max_frames, 1536]
        vision_embedding_2 = self.frame_feat_fc(vision_embedding_2)  # [bs, max_frames, hidden_size]

        embedding_2 = tf.concat([vision_embedding_2, bert_embedding_2], axis=1)
        embedding_2 = self.transformer(embedding_2)  # (bs, total_seq_len, d_model)
        embedding_2 = tf.transpose(embedding_2, perm=[0, 2, 1])  # (bs, d_model, total_seq_len)
        embedding_2 = self.multi_feat_fc(embedding_2)
        embedding_2 = tf.squeeze(embedding_2) # [bs, d_model]

        vision_embedding_2 = tf.transpose(vision_embedding_2, perm=[0, 2, 1])  # [bs, hidden_size, max_frames]
        vision_embedding_2 = self.frame_feat_fc_last(vision_embedding_2)  # [bs, hidden_size, 1]
        vision_embedding_2 = tf.squeeze(vision_embedding_2)

        bert_embedding_2 = tf.transpose(bert_embedding_2, perm=[0, 2, 1])  # [bs, hidden_size, bert_seq_length]
        bert_embedding_2 = self.text_feat_fc_last(bert_embedding_2)  # [bs, hidden_size, 1]
        bert_embedding_2 = tf.squeeze(bert_embedding_2)

        predictions_2 = self.dropout(embedding_2, training=training)
        predictions_2 = self.classifier(predictions_2)

        return embedding_1, embedding_2, predictions_1, predictions_2

    def get_variables(self):
        if not self.all_variables_1:  # is None, not initialized
            self.bert_variables_1 = self.bert.trainable_variables
            self.num_bert_1 = len(self.bert_variables_1)
            self.normal_variables_1 = self.text_feat_fc.trainable_variables + self.vision_transformer.trainable_variables + \
                                    self.frame_feat_fc.trainable_variables + self.transformer.trainable_variables + \
                                    self.multi_feat_fc.trainable_variables + self.frame_feat_fc_last.trainable_variables + \
                                    self.text_feat_fc_last.trainable_variables + self.classifier.trainable_variables
            self.all_variables_1 = self.bert_variables_1 + self.normal_variables_1
        return self.all_variables_1

    def optimize(self, gradients):
        bert_gradients_1 = gradients[:self.num_bert_1]
        self.bert_optimizer_1.apply_gradients(zip(bert_gradients_1, self.bert_variables_1))
        normal_gradients_1 = gradients[self.num_bert_1:]
        self.optimizer_1.apply_gradients(zip(normal_gradients_1, self.normal_variables_1))
