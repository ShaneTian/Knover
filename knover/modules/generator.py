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
"""Generator class"""

import numpy as np
import paddle
import paddle.fluid.layers as layers

import knover.modules.ops as ops
from knover.utils import str2bool


class Generator(object):
    """
    Generator class

    Use generator in inference phase.
    """

    @classmethod
    def add_cmdline_args(cls, parser):
        """Add cmdline arguments."""
        group = parser.add_argument_group("Generator")
        group.add_argument("--min_dec_len", type=int, default=1,
                           help="The minimum length of decoded sequence.")
        group.add_argument("--max_dec_len", type=int, default=64,
                           help="The maximum length of decoded sequence.")

        group.add_argument("--decoding_strategy", type=str, default="topk_sampling",
                           choices=["beam_search", "topk_sampling", "topp_sampling"],
                           help="The decoding strategy.")
        group.add_argument("--temperature", type=float, default=1.,
                           help="The temperature in each generation step.")
        group.add_argument("--ignore_unk", type=str2bool, default=True,
                           help="Whether to ignore UNK token in generation.")

        # multi sampling
        group.add_argument("--num_samples", type=int, default=None,
                           help="The number of sampling in generation. Multiple samples will rerank by a score.")

        # top-k sampling
        group.add_argument("--topk", type=int, default=10,
                           help="The hyper-parameter in top-k sampling..")

        # top-p sampling
        group.add_argument("--topp", type=float, default=0.9,
                           help="The hyper-parameter in top-p sampling.")

        # beam search
        group.add_argument("--beam_size", type=int, default=10,
                           help="The hyper-parameter in beam search.")
        group.add_argument("--length_average", type=str2bool, default=True,
                           help="The hyper-parameter in beam search.")
        group.add_argument("--length_penalty", type=float, default=0.0,
                           help="The hyper-parameter in beam search.")

        # n-gram blocking
        group.add_argument("--ngram_blocking", type=int, default=0,
                           help="N-gram blocking strategy.")

        return group

    def __init__(self, args):
        self.min_dec_len = args.min_dec_len
        self.max_dec_len = args.max_dec_len
        self.bos_id = args.bos_id
        self.eos_id = args.eos_id
        self.unk_id = args.unk_id
        self.mask_id = args.mask_id
        self.vocab_size = args.vocab_size

        # basic settings
        self.decoding_strategy = args.decoding_strategy
        self.ignore_unk = args.ignore_unk
        self.temperature = args.temperature

        # reranking
        self.num_samples = args.num_samples

        # top-k sampling
        self.topk = args.topk

        # top-p sampling
        self.topp = args.topp

        # beam search
        self.beam_size = args.beam_size
        self.length_penalty = args.length_penalty
        self.length_average = args.length_average

        # n-gram blocking
        self.ngram_blocking = args.ngram_blocking
        if self.ngram_blocking > 0:
            self.ngram_blocking_processor = ops.NGramBlockingProcessor(self.ngram_blocking, args.bos_id, self.eos_id)
        return

    def inference(self, model, inputs, outputs):
        """
        Run inference.

        Args:
            model: A generation model. Need to implement `_generation_network` and `_calc_logits`.
            inputs: A dict mapping input variable names to corresponding Variables.
            outputs: A dict mapping output variable name to corresponding Variables.

        Returns:
            predictions: A dict mapping keys to corresponding predictions.
        """
        # prepare while loop
        max_len = layers.fill_constant([1], "int64", self.max_dec_len, force_cpu=True)
        min_len = layers.fill_constant([1], "int64", self.min_dec_len, force_cpu=True)
        step_idx = layers.fill_constant([1], "int64", 0, force_cpu=True)

        if self.decoding_strategy == "beam_search":
            beam_size = self.beam_size
        else:
            beam_size = 1

        eos_penalty = np.zeros(self.vocab_size, dtype="float32")
        eos_penalty[self.eos_id] = -1e9
        eos_penalty = layers.assign(eos_penalty)

        token_penalty = np.zeros(self.vocab_size, dtype="float32")
        if self.ignore_unk:
            token_penalty[self.unk_id] = -1e9
        if self.mask_id is not None and self.mask_id >= 0:
            token_penalty[self.mask_id] = -1e9
        token_penalty = layers.assign(token_penalty)

        state = model._initialize_state(inputs, step_idx)
        if self.decoding_strategy == "beam_search":
            state["parent_idx"] = inputs["parent_idx"]

        if self.ngram_blocking > 0:
            self.ngram_blocking_processor.init(inputs["token_ids"])

        # start while loop
        cond = layers.less_than(x=step_idx, y=max_len)
        while_op = layers.While(cond)
        with while_op.block():
            model_input, pre_ids, pre_scores = model._prepare_timestep_input(state, step_idx)
            dec_out, _ = model._generation_network(**model_input)
            logits = model._calc_logits(dec_out)
            if model.dtype == "float16":
                logits = layers.cast(logits, "float32")

            logits = layers.elementwise_add(logits, token_penalty, axis=1)

            if self.ngram_blocking > 0:
                logits = self.ngram_blocking_processor.apply(logits, state["is_finished"])

            # min dec length
            min_len_cond = layers.less_than(x=step_idx, y=min_len)
            def min_len_penalty():
                """Plus minimum length penalty."""
                return layers.elementwise_add(logits, eos_penalty, axis=1)
            def no_penalty():
                """No penalty."""
                return logits
            logits = layers.case([(min_len_cond, min_len_penalty)], default=no_penalty)

            the_eos_penalty = state["is_finished"] * eos_penalty
            logits = logits - the_eos_penalty

            # get probs
            probs = layers.softmax(logits / self.temperature)
            if model.dtype == "float16":
                probs = layers.cast(probs, "float32")

            if self.decoding_strategy == "beam_search":
                topk_scores, topk_indices = layers.topk(
                    input=probs, k=beam_size)
            else:
                if self.decoding_strategy.startswith("sampling"):
                    sampling_ids = ops.sampling_id(probs)
                elif self.decoding_strategy.startswith("topk_sampling"):
                    topk_probs, _ = layers.topk(input=probs, k=self.topk)
                    ge_cond = layers.greater_equal(probs, topk_probs[:, -1:])
                    ge_cond = layers.cast(ge_cond, probs.dtype)
                    old_probs = probs
                    probs = probs * ge_cond / layers.reduce_sum(topk_probs, dim=-1, keep_dim=True)
                    sampling_ids = ops.sampling_id(probs)
                    probs = old_probs
                elif self.decoding_strategy.startswith("topp_sampling"):
                    sorted_probs, sorted_idx = layers.argsort(probs, descending=True)
                    cum_sorted_probs = layers.cumsum(sorted_probs, axis=1, exclusive=True)
                    lhs = cum_sorted_probs
                    rhs = layers.fill_constant_batch_size_like(
                        cum_sorted_probs,
                        cum_sorted_probs.shape,
                        cum_sorted_probs.dtype,
                        self.topp)
                    lt_cond = layers.less_than(lhs, rhs)
                    lt_cond = layers.cast(lt_cond, probs.dtype)
                    old_probs = probs
                    candidate_probs = sorted_probs * lt_cond
                    probs = candidate_probs / layers.reduce_sum(candidate_probs, dim=-1, keep_dim=True)
                    sampling_ids = ops.sampling_id(probs)
                    sampling_ids = paddle.index_sample(sorted_idx, layers.unsqueeze(sampling_ids, [1]))
                    sampling_ids = layers.squeeze(sampling_ids, [1])
                    probs = old_probs
                else:
                    raise ValueError(self.decoding_strategy)

                sampling_scores = layers.one_hot(layers.unsqueeze(sampling_ids, [1]), self.vocab_size)
                sampling_scores = sampling_scores * probs - (1 - sampling_scores) * 1e3
                topk_scores, topk_indices = layers.topk(sampling_scores, k=1)

            pre_len = layers.cast(step_idx, "float32")
            layers.increment(x=step_idx, value=1.0, in_place=True)
            cur_len = layers.cast(step_idx, "float32")

            # avoid nan in beam_search
            small_prob_cond = layers.cast(topk_scores < 1e-9, topk_scores.dtype)
            topk_scores = topk_scores * (1 - small_prob_cond) + 1e-9 * small_prob_cond
            topk_scores = layers.log(topk_scores)

            # update scores
            if self.length_average:
                accu_scores = layers.elementwise_add(
                    x=topk_scores, y=pre_scores * pre_len, axis=0) / cur_len
            elif self.length_penalty > 0:
                pre_lp = layers.pow((5 + pre_len) / 6, self.length_penalty)
                cur_lp = layers.pow((5 + cur_len) / 6, self.length_penalty)
                accu_scores = layers.elementwise_add(
                    x=topk_scores, y=pre_scores * pre_lp, axis=0) / cur_lp
            else:
                accu_scores = layers.elementwise_add(
                    x=topk_scores, y=pre_scores, axis=0)
            x = layers.elementwise_mul(accu_scores, 1 - state["is_finished"], axis=0)
            y = layers.elementwise_mul(pre_scores, state["is_finished"], axis=0)
            accu_scores = layers.elementwise_add(x, y, axis=0)
            accu_scores = accu_scores * (1 - small_prob_cond) - 1e9 * small_prob_cond
            topk_indices = layers.lod_reset(topk_indices, pre_ids)
            accu_scores = layers.lod_reset(accu_scores, pre_ids)
            selected_ids, selected_scores, parent_idx = layers.beam_search(
                pre_ids=pre_ids,
                pre_scores=pre_scores,
                ids=topk_indices,
                scores=accu_scores,
                beam_size=beam_size,
                end_id=-1,
                return_parent_idx=True)

            if self.decoding_strategy == "beam_search":
                layers.assign(parent_idx, state["parent_idx"])
            state = model._update_state(
                state,
                model_input,
                selected_ids,
                selected_scores,
                step_idx)

            if self.ngram_blocking > 0:
                if self.decoding_strategy == "beam_search":
                    self.ngram_blocking_processor.update(selected_ids, state["is_finished"], parent_idx)
                else:
                    self.ngram_blocking_processor.update(selected_ids, state["is_finished"])

            length_cond = layers.less_than(x=step_idx, y=max_len)
            finish_cond = layers.logical_not(layers.reduce_all(layers.cast(state["is_finished"], "bool")))
            layers.logical_and(x=length_cond, y=finish_cond, out=cond)

        finished_ids, finished_scores = layers.beam_search_decode(
            state["tgt_ids"], state["scores"], beam_size=beam_size, end_id=self.eos_id)

        predictions = {
            "finished_ids": finished_ids,
            "finished_scores": finished_scores,
            "token_ids": inputs["token_ids"],
            "data_id": inputs["data_id"],
            "pos_ids": state["tgt_pos"],
            "generation_mask": state["tgt_generation_mask"],
        }
        return predictions
