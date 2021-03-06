import tensorflow as tf
from tensorflow.python.keras.models import Model
from transformers import TFBertModel, create_optimizer
from transformers.models.bert.modeling_tf_bert import TFBertMLMHead
from bert import TFBertModel_MM, shape_list
from roformer import TFRoFormerModel_MM, TFRoFormerMLMHead
# from layers.transformer_layer import TransformerEncoder


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
        self.activation_bn = tf.keras.layers.BatchNormalization() # modify bn

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
        activation = self.activation_bn(activation) # modify bn
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


class SoftDBoF(tf.keras.layers.Layer):
    def __init__(self, feature_size,cluster_size,dropout,output_size):
        super().__init__()
        self.feature_size = feature_size
        # self.add_batch_norm = add_batch_norm
        self.cluster_size = cluster_size
        # self.max_pool = max_pool
        self.activation_bn = tf.keras.layers.BatchNormalization()
        self.cluster_dense1 = tf.keras.layers.Dense(self.cluster_size, activation=None, use_bias=False)
        self.dense = tf.keras.layers.Dense(self.feature_size)
        self.dropout = tf.keras.layers.Dropout(rate=dropout, seed=1)
        self.fc = tf.keras.layers.Dense(output_size, activation=None)

    def build(self, input_shape):
        self.cluster_weights2 = self.add_weight(name="cluster_weights2",
                                                shape=(2, self.feature_size, self.cluster_size),
                                                initializer=tf.keras.initializers.glorot_normal, trainable=True)
        self.built = True

    def call(self, inputs, training):
        image_embeddings, mask = inputs
        _, num_segments, _ = image_embeddings.shape
        if mask is not None:  # in case num of images is less than num_segments
            images_mask = tf.sequence_mask(mask, maxlen=num_segments)
            images_mask = tf.cast(tf.expand_dims(images_mask, -1), tf.float32)
            image_embeddings = tf.multiply(image_embeddings, images_mask)
        image_embeddings = self.dense(image_embeddings) # b,32,1024
        reshaped_input = tf.reshape(image_embeddings, [-1, self.feature_size])
        activation = self.cluster_dense1(reshaped_input)
        activation = self.activation_bn(activation)
        activation = tf.nn.softmax(activation, axis=-1)
        activation = tf.reshape(activation, [-1, num_segments, self.cluster_size])
        activation_sum = tf.reduce_sum(activation,1)
        activation_sum = tf.nn.l2_normalize(activation_sum,1)
        activation_max = tf.reduce_max(activation,1)
        activation_max = tf.nn.l2_normalize(activation_max,1)
        activation = tf.concat([activation_sum,activation_max],1) # b,cluster_size*2

        output = self.dropout(activation)
        output = self.fc(output)
        return output

class NextSoftDBoF(tf.keras.layers.Layer):
    def __init__(self, feature_size,cluster_size,dropout,output_size,groups=4,expansion=2):
        super().__init__()
        self.feature_size = feature_size
        # self.add_batch_norm = add_batch_norm
        self.cluster_size = cluster_size
        self.groups = groups
        self.expansion = expansion
        # self.max_pool = max_pool
        self.activation_bn = tf.keras.layers.BatchNormalization()
        self.expand_dense = tf.keras.layers.Dense(self.expansion * self.feature_size)
        self.cluster_dense1 = tf.keras.layers.Dense(self.groups * self.cluster_size, activation=None, use_bias=False)
        self.attention_dense = tf.keras.layers.Dense(self.groups, activation=tf.nn.sigmoid)
        self.dropout = tf.keras.layers.Dropout(rate=dropout, seed=1)
        self.fc = tf.keras.layers.Dense(output_size, activation=None)

    def build(self, input_shape):
        self.cluster_weights2 = self.add_weight(name="cluster_weights2",
                                                shape=(2, self.feature_size, self.cluster_size),
                                                initializer=tf.keras.initializers.glorot_normal, trainable=True)
        self.built = True

    def call(self, inputs, training):
        image_embeddings, mask = inputs
        _, num_segments, _ = image_embeddings.shape
        if mask is not None:  # in case num of images is less than num_segments
            images_mask = tf.sequence_mask(mask, maxlen=num_segments)
            images_mask = tf.cast(tf.expand_dims(images_mask, -1), tf.float32)
            image_embeddings = tf.multiply(image_embeddings, images_mask)
        image_embeddings = self.expand_dense(image_embeddings) # b,32,2*1536
        reshaped_input = tf.reshape(image_embeddings, [-1, self.expansion * self.feature_size]) # b*32,2*1536

        attention = self.attention_dense(image_embeddings) # b,32,8
        attention = tf.reshape(attention, [-1, num_segments * self.groups, 1]) # b, 32*g, 1

        activation = self.cluster_dense1(reshaped_input) # b*32,g*c
        activation = self.activation_bn(activation) 
        activation = tf.nn.softmax(activation, axis=-1)
        # activation = tf.reshape(activation, [-1, num_segments, self.cluster_size])
        activation = tf.reshape(activation, [-1, num_segments * self.groups, self.cluster_size]) # b, 32*g, c
        activation = tf.multiply(activation, attention)
        activation_sum = tf.reduce_sum(activation,1)
        activation_sum = tf.nn.l2_normalize(activation_sum,1)
        # activation_max = tf.reduce_max(activation,1)
        # activation_max = tf.nn.l2_normalize(activation_max,1)
        # activation = tf.concat([activation_sum,activation_max],1) # b,cluster_size*2

        output = self.dropout(activation_sum)
        output = self.fc(output)
        return output


class Video_transformer(tf.keras.layers.Layer):
    def __init__(self, num_hidden_layers=1, output_size=1024, dropout=0.2):
        super().__init__()
        self.fc = tf.keras.layers.Dense(output_size, activation='relu')
        self.frame_tf_encoder = TransformerEncoder(hidden_size=output_size, num_hidden_layers=num_hidden_layers,
                 num_attention_heads=8, intermediate_size=3072)

    def call(self, inputs, **kwargs):
        image_embeddings, mask = inputs
        image_embeddings = self.fc(image_embeddings)
        all_layer_outputs, all_attention_probs = self.frame_tf_encoder(image_embeddings)
        return all_layer_outputs[-1]



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
        self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
                                 output_size=config.vlad_hidden_size, dropout=config.dropout)
        self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.bert_map = tf.keras.layers.Dense(1024, activation ='relu')

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, **kwargs):
        bert_embedding = self.bert([inputs['input_ids'], inputs['mask']])[1]
        bert_embedding = self.bert_map(bert_embedding)
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        vision_embedding = self.nextvlad([inputs['frames'], frame_num])
        vision_embedding = vision_embedding * tf.cast(tf.expand_dims(frame_num, -1) > 0, tf.float32)
        final_embedding = self.fusion([vision_embedding, bert_embedding])
        predictions = self.classifier(final_embedding)

        return predictions, final_embedding, vision_embedding, bert_embedding

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.nextvlad.trainable_variables + self.fusion.trainable_variables + \
                                    self.classifier.trainable_variables + self.bert_map.trainable_variables # ????????????????????????
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))


class MultiModal_soft(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel.from_pretrained(config.bert_dir)
        self.softdbof = SoftDBoF(config.frame_embedding_size,config.vlad_cluster_size,
                                dropout=config.dropout,output_size=config.vlad_hidden_size)
        self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.bert_map = tf.keras.layers.Dense(1024, activation ='relu')

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, **kwargs):
        bert_embedding = self.bert([inputs['input_ids'], inputs['mask']])[1]
        bert_embedding = self.bert_map(bert_embedding)
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        vision_embedding = self.softdbof([inputs['frames'], frame_num])
        vision_embedding = vision_embedding * tf.cast(tf.expand_dims(frame_num, -1) > 0, tf.float32)
        final_embedding = self.fusion([vision_embedding, bert_embedding])
        predictions = self.classifier(final_embedding)

        return predictions, final_embedding, vision_embedding, bert_embedding

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.softdbof.trainable_variables + self.fusion.trainable_variables + \
                                    self.classifier.trainable_variables + self.bert_map.trainable_variables # ????????????????????????
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))

class MultiModal_nextsoft(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel.from_pretrained(config.bert_dir)
        self.nextsoftdbof = NextSoftDBoF(config.frame_embedding_size,config.vlad_cluster_size,
                                dropout=config.dropout,output_size=config.vlad_hidden_size,groups=config.vlad_groups)
        self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.bert_map = tf.keras.layers.Dense(1024, activation ='relu')

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, **kwargs):
        bert_embedding = self.bert([inputs['input_ids'], inputs['mask']])[1]
        bert_embedding = self.bert_map(bert_embedding)
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        vision_embedding = self.nextsoftdbof([inputs['frames'], frame_num])
        vision_embedding = vision_embedding * tf.cast(tf.expand_dims(frame_num, -1) > 0, tf.float32)
        final_embedding = self.fusion([vision_embedding, bert_embedding])
        predictions = self.classifier(final_embedding)

        return predictions, final_embedding, vision_embedding, bert_embedding

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.nextsoftdbof.trainable_variables + self.fusion.trainable_variables + \
                                    self.classifier.trainable_variables + self.bert_map.trainable_variables # ????????????????????????
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))


class MultiModal_mlm(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel.from_pretrained(config.bert_dir)
        # mlm head
        bert_config = self.bert.config
        self.mlm = TFBertMLMHead(bert_config, input_embeddings=self.bert.bert.embeddings, name="mlm___cls")

        self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
                                 output_size=config.vlad_hidden_size, dropout=config.dropout)
        self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.bert_map = tf.keras.layers.Dense(1024, activation ='relu')

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, training, **kwargs):
        bert_output = self.bert([inputs['input_ids'], inputs['mask']]) # inputs have random mask
        sequence_output = bert_output[0]
        bert_embedding = bert_output[1]
        prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)
        # loss_mlm = (
        #     None if inputs["mask_labels"] is None else self.compute_loss(labels=inputs["mask_labels"], logits=prediction_scores)
        # )
        bert_embedding = self.bert_map(bert_embedding)
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        vision_embedding = self.nextvlad([inputs['frames'], frame_num])
        vision_embedding = vision_embedding * tf.cast(tf.expand_dims(frame_num, -1) > 0, tf.float32)
        final_embedding = self.fusion([vision_embedding, bert_embedding])
        predictions = self.classifier(final_embedding)

        return predictions, final_embedding, vision_embedding, bert_embedding, prediction_scores_mlm

    def compute_loss(self, labels, logits):
        loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, reduction=tf.keras.losses.Reduction.NONE
        )
        # make sure only labels that are not equal to -100 affect the loss
        active_loss = tf.not_equal(tf.reshape(labels, (-1,)), -100)
        reduced_logits = tf.boolean_mask(tf.reshape(logits, (-1, shape_list(logits)[2])), active_loss)
        labels = tf.boolean_mask(tf.reshape(labels, (-1,)), active_loss)
        return loss_fn(labels, reduced_logits)

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.nextvlad.trainable_variables + self.fusion.trainable_variables + \
                                    self.classifier.trainable_variables + self.bert_map.trainable_variables + \
                                    self.mlm.trainable_variables
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))


class Uniter(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel_MM.from_pretrained(config.bert_dir)
        tf.print(self.bert)
        # mlm head
        bert_config = self.bert.config
        self.mlm = TFBertMLMHead(bert_config, input_embeddings=self.bert.bert.embeddings, name="mlm___cls")

        # self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
        #                          output_size=config.vlad_hidden_size, dropout=config.dropout)
        # self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.frame_map = tf.keras.layers.Dense(768, activation ='relu')
        self.fc = tf.keras.layers.Dense(config.hidden_size)
        self.pooling = config.uniter_pooling

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, training, **kwargs):
        """ original
        # image_embedding = inputs['frames']
        # _, num_segments, _ = image_embedding.shape
        # image_embedding = self.frame_map(image_embedding) # b,32,768
        # frame_num = tf.reshape(inputs['num_frames'], [-1])
        # images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        # images_mask = tf.cast(images_mask, tf.int32)
        # # import pdb;pdb.set_trace()
        # _, seq_len = inputs['input_ids'].shape
        # bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        # sequence_output = bert_output[0]
        # bert_embedding = bert_output[1]
        # prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        # predictions = self.classifier(bert_embedding)
        """

        image_embedding = inputs['frames']
        _, num_segments, _ = image_embedding.shape
        image_embedding = self.frame_map(image_embedding) # b,32,768
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        images_mask = tf.cast(images_mask, tf.int32)
        # import pdb;pdb.set_trace()
        _, seq_len = inputs['input_ids'].shape
        bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        sequence_output = bert_output[0]
        sequence_output = self.fc(sequence_output)
        if self.pooling == 'cls':
            bert_embedding = sequence_output[:,0]
        elif self.pooling == 'mean':
            bert_embedding = tf.reduce_mean(sequence_output, 1)
        elif self.pooling == 'max':
            text_mask = 1-tf.cast(inputs['mask'], tf.int32)
            neg_mask = tf.concat([text_mask, 1-images_mask], 1)
            super_neg = tf.expand_dims(tf.cast(neg_mask, tf.float32), axis=2) * -1000
            bert_embedding = tf.reduce_max(sequence_output + super_neg, 1)
        prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        predictions = self.classifier(bert_embedding)

        return predictions, bert_embedding, prediction_scores_mlm

    def compute_loss(self, labels, logits):
        loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, reduction=tf.keras.losses.Reduction.NONE
        )
        # make sure only labels that are not equal to -100 affect the loss
        active_loss = tf.not_equal(tf.reshape(labels, (-1,)), -100)
        reduced_logits = tf.boolean_mask(tf.reshape(logits, (-1, shape_list(logits)[2])), active_loss)
        labels = tf.boolean_mask(tf.reshape(labels, (-1,)), active_loss)
        return loss_fn(labels, reduced_logits)

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.classifier.trainable_variables + self.frame_map.trainable_variables + \
                                    self.mlm.trainable_variables + self.fc.trainable_variables
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))

class Uniter_vlad(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFBertModel_MM.from_pretrained(config.bert_dir)
        tf.print(self.bert)
        # mlm head
        bert_config = self.bert.config
        self.mlm = TFBertMLMHead(bert_config, input_embeddings=self.bert.bert.embeddings, name="mlm___cls")

        self.nextvlad = NeXtVLAD(768, config.vlad_cluster_size,
                                 output_size=config.hidden_size, dropout=config.dropout)
        # self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.frame_map = tf.keras.layers.Dense(768, activation ='relu')
        # self.fc = tf.keras.layers.Dense(config.hidden_size)
        self.pooling = config.uniter_pooling

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, training, **kwargs):
        """ original
        # image_embedding = inputs['frames']
        # _, num_segments, _ = image_embedding.shape
        # image_embedding = self.frame_map(image_embedding) # b,32,768
        # frame_num = tf.reshape(inputs['num_frames'], [-1])
        # images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        # images_mask = tf.cast(images_mask, tf.int32)
        # # import pdb;pdb.set_trace()
        # _, seq_len = inputs['input_ids'].shape
        # bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        # sequence_output = bert_output[0]
        # bert_embedding = bert_output[1]
        # prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        # predictions = self.classifier(bert_embedding)
        """

        image_embedding = inputs['frames']
        _, num_segments, _ = image_embedding.shape
        image_embedding = self.frame_map(image_embedding) # b,32,768
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        images_mask = tf.cast(images_mask, tf.int32)
        # import pdb;pdb.set_trace()
        _, seq_len = inputs['input_ids'].shape
        bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        sequence_output = bert_output[0] # b,32+32,768

        bert_embedding = self.nextvlad([sequence_output,None])
        prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        predictions = self.classifier(bert_embedding)

        return predictions, bert_embedding, prediction_scores_mlm

    def compute_loss(self, labels, logits):
        loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, reduction=tf.keras.losses.Reduction.NONE
        )
        # make sure only labels that are not equal to -100 affect the loss
        active_loss = tf.not_equal(tf.reshape(labels, (-1,)), -100)
        reduced_logits = tf.boolean_mask(tf.reshape(logits, (-1, shape_list(logits)[2])), active_loss)
        labels = tf.boolean_mask(tf.reshape(labels, (-1,)), active_loss)
        return loss_fn(labels, reduced_logits)

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.classifier.trainable_variables + self.frame_map.trainable_variables + \
                                    self.mlm.trainable_variables + self.nextvlad.trainable_variables
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))


class Uniter_roformer(Model):
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bert = TFRoFormerModel_MM.from_pretrained(config.bert_dir)
        tf.print(self.bert)
        # mlm head
        bert_config = self.bert.config
        self.mlm = TFRoFormerMLMHead(bert_config, input_embeddings=self.bert.roformer.embeddings, name="mlm___cls")

        # self.nextvlad = NeXtVLAD(config.frame_embedding_size, config.vlad_cluster_size,
        #                          output_size=config.vlad_hidden_size, dropout=config.dropout)
        # self.fusion = ConcatDenseSE(config.hidden_size, config.se_ratio)
        self.num_labels = config.num_labels
        self.classifier = tf.keras.layers.Dense(self.num_labels, activation='sigmoid')
        self.frame_map = tf.keras.layers.Dense(768, activation ='relu')
        self.fc = tf.keras.layers.Dense(config.hidden_size)
        self.pooling = config.uniter_pooling

        self.bert_optimizer, self.bert_lr = create_optimizer(init_lr=config.bert_lr,
                                                             num_train_steps=config.bert_total_steps,
                                                             num_warmup_steps=config.bert_warmup_steps)
        self.optimizer, self.lr = create_optimizer(init_lr=config.lr,
                                                   num_train_steps=config.total_steps,
                                                   num_warmup_steps=config.warmup_steps)
        self.bert_variables, self.num_bert, self.normal_variables, self.all_variables = None, None, None, None

    def call(self, inputs, training, **kwargs):
        """ original
        # image_embedding = inputs['frames']
        # _, num_segments, _ = image_embedding.shape
        # image_embedding = self.frame_map(image_embedding) # b,32,768
        # frame_num = tf.reshape(inputs['num_frames'], [-1])
        # images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        # images_mask = tf.cast(images_mask, tf.int32)
        # # import pdb;pdb.set_trace()
        # _, seq_len = inputs['input_ids'].shape
        # bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        # sequence_output = bert_output[0]
        # bert_embedding = bert_output[1]
        # prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        # predictions = self.classifier(bert_embedding)
        """

        image_embedding = inputs['frames']
        _, num_segments, _ = image_embedding.shape
        image_embedding = self.frame_map(image_embedding) # b,32,768
        frame_num = tf.reshape(inputs['num_frames'], [-1])
        images_mask = tf.sequence_mask(frame_num, maxlen=num_segments)
        images_mask = tf.cast(images_mask, tf.int32)
        # import pdb;pdb.set_trace()
        _, seq_len = inputs['input_ids'].shape
        bert_output = self.bert(input_ids=inputs['input_ids'], attention_mask=inputs['mask'], frame_features=image_embedding, frame_attention_mask=images_mask) # inputs have random mask
        sequence_output = bert_output[0]
        sequence_output = self.fc(sequence_output)
        if self.pooling == 'cls':
            bert_embedding = sequence_output[:,0]
        elif self.pooling == 'mean':
            bert_embedding = tf.reduce_mean(sequence_output, 1)
        elif self.pooling == 'max':
            text_mask = 1-tf.cast(inputs['mask'], tf.int32)
            neg_mask = tf.concat([text_mask, 1-images_mask], 1)
            super_neg = tf.expand_dims(tf.cast(neg_mask, tf.float32), axis=2) * -1000
            bert_embedding = tf.reduce_max(sequence_output + super_neg, 1)
        prediction_scores_mlm = self.mlm(sequence_output=sequence_output, training=training)[:,:seq_len]

        predictions = self.classifier(bert_embedding)

        return predictions, bert_embedding, prediction_scores_mlm
    
    def compute_loss(self, labels, logits):
        loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, reduction=tf.keras.losses.Reduction.NONE
        )
        # make sure only labels that are not equal to -100 affect the loss
        active_loss = tf.not_equal(tf.reshape(labels, (-1,)), -100)
        reduced_logits = tf.boolean_mask(tf.reshape(logits, (-1, shape_list(logits)[2])), active_loss)
        labels = tf.boolean_mask(tf.reshape(labels, (-1,)), active_loss)
        return loss_fn(labels, reduced_logits)

    def get_variables(self):
        if not self.all_variables:  # is None, not initialized
            self.bert_variables = self.bert.trainable_variables
            self.num_bert = len(self.bert_variables)
            self.normal_variables = self.classifier.trainable_variables + self.frame_map.trainable_variables + \
                                    self.mlm.trainable_variables + self.fc.trainable_variables
            self.all_variables = self.bert_variables + self.normal_variables
        return self.all_variables

    def optimize(self, gradients):
        bert_gradients = gradients[:self.num_bert]
        self.bert_optimizer.apply_gradients(zip(bert_gradients, self.bert_variables))
        normal_gradients = gradients[self.num_bert:]
        self.optimizer.apply_gradients(zip(normal_gradients, self.normal_variables))
