#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""NSP Reader."""

from collections import namedtuple

import numpy as np

from knover.data.dialog_reader import DialogReader
from knover.utils import mask, pad_batch_data, str2bool


class NSPReader(DialogReader):
    """NSP Reader."""

    @classmethod
    def add_cmdline_args(cls, parser):
        """Add cmdline arguments."""
        group = DialogReader.add_cmdline_args(parser)
        group.add_argument("--mix_negative_sample", type=str2bool, default=False,
                           help="Whether to mix random negative samples into dataset.")
        group.add_argument("--neg_pool_size", type=int, default=2 ** 16,
                           help="The size of random negative pool.")
        return group

    def __init__(self, args):
        super(NSPReader, self).__init__(args)
        self.fields.append("label")
        self.Record = namedtuple("Record", self.fields, defaults=(None,) * len(self.fields))

        self.mix_negative_sample = args.mix_negative_sample
        self.neg_pool_size = args.neg_pool_size
        return

    def _convert_example_to_record(self, example, is_infer):
        """Convert example to record which can be used as the model's input."""
        record = super(NSPReader, self)._convert_example_to_record(example, False)
        if "label" in example._fields:
            record = record._replace(label=int(example.label))
        return record

    def _mix_negative_sample(self, reader, neg_pool_size=2 ** 16):
        """Mix random negative samples into dataset."""
        def _gen_from_pool(pool):
            """Generate negative samples from pool."""
            num_samples = len(pool)
            if num_samples == 1:
                # it is impossible to generate negative sample when the pool has only one sample
                yield pool[0]._replace(label=1)
                return
            self.global_rng.shuffle(pool)
            for i in range(num_samples):
                pool[i] = pool[i]._replace(label=1)
                j = (i + 1) % num_samples
                idx_i = pool[i].tgt_start_idx
                idx_j = pool[j].tgt_start_idx
                field_values = {}
                field_values["token_ids"] = pool[i].token_ids[:idx_i] + pool[j].token_ids[idx_j:]
                field_values["type_ids"] = pool[i].type_ids[:idx_i] + pool[j].type_ids[idx_j:]
                if self.position_style == "continuous":
                    field_values["pos_ids"] = list(range(len(field_values["token_ids"])))
                else:
                    field_values["pos_ids"] = pool[i].pos_ids[:idx_i] + pool[j].pos_ids[idx_j:]
                if self.use_role:
                    field_values["role_ids"] = pool[i].role_ids[:idx_i] + pool[j].role_ids[idx_j:]
                neg_record = self.Record(
                    **field_values,
                    tgt_start_idx=idx_i,
                    data_id=-1,
                    label=0
                )
                pool.append(neg_record)
            self.global_rng.shuffle(pool)
            for record in pool:
                yield record

        def __wrapper__():
            pool = []
            for record in reader():
                pool.append(record)
                if len(pool) == neg_pool_size:
                    for record in _gen_from_pool(pool):
                        yield record
                    pool = []
            if len(pool) > 0:
                for record in _gen_from_pool(pool):
                    yield record
        return __wrapper__

    def _batch_reader(self, reader, phase=None, is_infer=False):
        """Construct a batch reader from a record reader."""
        if self.mix_negative_sample:
            reader = self._mix_negative_sample(reader, self.neg_pool_size)
        return super(NSPReader, self)._batch_reader(
            reader,
            phase=phase,
            is_infer=is_infer)

    def _pad_batch_records(self, batch_records, is_infer, phase=None):
        """Padding a batch of records and construct model's inputs."""
        batch = {}
        batch_token_ids = [record.token_ids for record in batch_records]
        batch_type_ids = [record.type_ids for record in batch_records]
        batch_pos_ids = [record.pos_ids for record in batch_records]
        if self.use_role:
            batch_role_ids = [record.role_ids for record in batch_records]
        batch_tgt_start_idx = [record.tgt_start_idx for record in batch_records]
        batch_label = [record.label for record in batch_records]

        batch_mask_token_ids, tgt_label, tgt_idx, label_idx = mask(
            batch_tokens=batch_token_ids,
            vocab_size=self.vocab_size,
            bos_id=self.bos_id,
            eos_id=self.eos_id,
            mask_id=self.mask_id,
            tgt_starts=batch_tgt_start_idx,
            labels=batch_label,
            is_unidirectional=False)
        if not is_infer:
            batch_token_ids = batch_mask_token_ids
        batch["token_ids"] = pad_batch_data(batch_token_ids, pad_id=self.pad_id)
        batch["type_ids"] = pad_batch_data(batch_type_ids, pad_id=0)
        batch["pos_ids"] = pad_batch_data(batch_pos_ids, pad_id=0)
        if self.use_role:
            batch["role_ids"] = pad_batch_data(batch_role_ids, pad_id=0)
        attention_mask = self._gen_self_attn_mask(batch_token_ids, is_unidirectional=False)

        batch["attention_mask"] = attention_mask
        batch["label_idx"] = label_idx

        if not is_infer:
            batch_label = np.array(batch_label).astype("int64").reshape([-1, 1])
            batch["label"] = batch_label
            batch["tgt_label"] = tgt_label
            batch["tgt_idx"] = tgt_idx
        else:
            batch_data_id = [record.data_id for record in batch_records]
            batch["data_id"] = np.array(batch_data_id).astype("int64").reshape([-1, 1])

        return batch
