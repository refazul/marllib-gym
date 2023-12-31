# MIT License

# Copyright (c) 2023 Replicable-MARL

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import collections
import logging
import numpy as np
import platform
import random
from typing import Any, Dict, List, Optional

# Import ray before psutil will make sure we use psutil's bundled version
import ray  # noqa F401
import psutil  # noqa E402

from ray.rllib.execution.segment_tree import SumSegmentTree, MinSegmentTree
from ray.rllib.policy.rnn_sequencing import \
    timeslice_along_seq_lens_with_overlap
from ray.rllib.policy.sample_batch import SampleBatch, MultiAgentBatch, \
    DEFAULT_POLICY_ID
from ray.rllib.utils.annotations import DeveloperAPI, override
from ray.util.iter import ParallelIteratorWorker
from ray.util.debug import log_once
from ray.rllib.utils.annotations import Deprecated
from ray.rllib.utils.deprecation import DEPRECATED_VALUE, deprecation_warning
from ray.rllib.utils.timer import TimerStat
from ray.rllib.utils.window_stat import WindowStat
from ray.rllib.utils.typing import SampleBatchType

# Constant that represents all policies in lockstep replay mode.
_ALL_POLICIES = "__all__"

logger = logging.getLogger(__name__)


def warn_replay_capacity(*, item: SampleBatchType, num_items: int) -> None:
    """Warn if the configured replay buffer capacity is too large."""
    if log_once("replay_capacity"):
        item_size = item.size_bytes()
        psutil_mem = psutil.virtual_memory()
        total_gb = psutil_mem.total / 1e9
        mem_size = num_items * item_size / 1e9
        msg = ("Estimated max memory usage for replay buffer is {} GB "
               "({} batches of size {}, {} bytes each), "
               "available system memory is {} GB".format(
            mem_size, num_items, item.count, item_size, total_gb))
        if mem_size > total_gb:
            raise ValueError(msg)
        elif mem_size > 0.2 * total_gb:
            logger.warning(msg)
        else:
            logger.info(msg)


@Deprecated(new="warn_replay_capacity", error=False)
def warn_replay_buffer_size(*, item: SampleBatchType, num_items: int) -> None:
    return warn_replay_capacity(item=item, num_items=num_items)


@DeveloperAPI
class ReplayBuffer:
    @DeveloperAPI
    def __init__(self,
                 capacity: int = 10000,
                 size: Optional[int] = DEPRECATED_VALUE):
        """Initializes a Replaybuffer instance.

        Args:
            capacity (int): Max number of timesteps to store in the FIFO
                buffer. After reaching this number, older samples will be
                dropped to make space for new ones.
        """
        # Deprecated args.
        if size != DEPRECATED_VALUE:
            deprecation_warning(
                "ReplayBuffer(size)", "ReplayBuffer(capacity)", error=False)
            capacity = size

        # The actual storage (list of SampleBatches).
        self._storage = []

        self.capacity = capacity
        # The next index to override in the buffer.
        self._next_idx = 0
        self._hit_count = np.zeros(self.capacity)

        # Whether we have already hit our capacity (and have therefore
        # started to evict older samples).
        self._eviction_started = False

        self._num_timesteps_added = 0
        self._num_timesteps_added_wrap = 0
        self._num_timesteps_sampled = 0
        self._evicted_hit_stats = WindowStat("evicted_hit", 1000)
        self._est_size_bytes = 0

    def __len__(self) -> int:
        return len(self._storage)

    @DeveloperAPI
    def add(self, item: SampleBatchType, weight: float) -> None:
        assert item.count > 0, item
        warn_replay_capacity(item=item, num_items=self.capacity / item.count)

        self._num_timesteps_added += item.count
        self._num_timesteps_added_wrap += item.count

        if self._next_idx >= len(self._storage):
            self._storage.append(item)
            self._est_size_bytes += item.size_bytes()
        else:
            self._storage[self._next_idx] = item

        # Wrap around storage as a circular buffer once we hit capacity.
        if self._num_timesteps_added_wrap >= self.capacity:
            self._eviction_started = True
            self._num_timesteps_added_wrap = 0
            self._next_idx = 0
        else:
            self._next_idx += 1

        if self._eviction_started:
            self._evicted_hit_stats.push(self._hit_count[self._next_idx])
            self._hit_count[self._next_idx] = 0

    def _encode_sample(self, idxes: List[int]) -> SampleBatchType:
        out = SampleBatch.concat_samples([self._storage[i] for i in idxes])
        out.decompress_if_needed()
        return out

    @DeveloperAPI
    def sample(self, num_items: int) -> SampleBatchType:
        """Sample a batch of experiences.

        Args:
            num_items (int): Number of items to sample from this buffer.

        Returns:
            SampleBatchType: concatenated batch of items.
        """
        idxes = [
            random.randint(0,
                           len(self._storage) - 1) for _ in range(num_items)
        ]
        self._num_sampled += num_items
        return self._encode_sample(idxes)

    @DeveloperAPI
    def stats(self, debug=False) -> dict:
        data = {
            "added_count": self._num_timesteps_added,
            "added_count_wrapped": self._num_timesteps_added_wrap,
            "eviction_started": self._eviction_started,
            "sampled_count": self._num_timesteps_sampled,
            "est_size_bytes": self._est_size_bytes,
            "num_entries": len(self._storage),
        }
        if debug:
            data.update(self._evicted_hit_stats.stats())
        return data

    @DeveloperAPI
    def get_state(self) -> Dict[str, Any]:
        """Returns all local state.

        Returns:
            Dict[str, Any]: The serializable local state.
        """
        state = {"_storage": self._storage, "_next_idx": self._next_idx}
        state.update(self.stats(debug=False))
        return state

    @DeveloperAPI
    def set_state(self, state: Dict[str, Any]) -> None:
        """Restores all local state to the provided `state`.

        Args:
            state (Dict[str, Any]): The new state to set this buffer. Can be
                obtained by calling `self.get_state()`.
        """
        # The actual storage.
        self._storage = state["_storage"]
        self._next_idx = state["_next_idx"]
        # Stats and counts.
        self._num_timesteps_added = state["added_count"]
        self._num_timesteps_added_wrap = state["added_count_wrapped"]
        self._eviction_started = state["eviction_started"]
        self._num_timesteps_sampled = state["sampled_count"]
        self._est_size_bytes = state["est_size_bytes"]


@DeveloperAPI
class PrioritizedReplayBuffer(ReplayBuffer):
    @DeveloperAPI
    def __init__(self,
                 capacity: int = 10000,
                 alpha: float = 1.0,
                 size: Optional[int] = DEPRECATED_VALUE):
        """Initializes a PrioritizedReplayBuffer instance.

        Args:
            capacity (int): Max number of timesteps to store in the FIFO
                buffer. After reaching this number, older samples will be
                dropped to make space for new ones.
            alpha (float): How much prioritization is used
                (0.0=no prioritization, 1.0=full prioritization).
        """
        super(PrioritizedReplayBuffer, self).__init__(capacity, size)
        assert alpha > 0
        self._alpha = alpha

        it_capacity = 1
        while it_capacity < self.capacity:
            it_capacity *= 2

        self._it_sum = SumSegmentTree(it_capacity)
        self._it_min = MinSegmentTree(it_capacity)
        self._max_priority = 1.0
        self._prio_change_stats = WindowStat("reprio", 1000)

    @DeveloperAPI
    @override(ReplayBuffer)
    def add(self, item: SampleBatchType, weight: float) -> None:
        idx = self._next_idx
        super(PrioritizedReplayBuffer, self).add(item, weight)
        if weight is None:
            weight = self._max_priority
        self._it_sum[idx] = weight ** self._alpha
        self._it_min[idx] = weight ** self._alpha

    def _sample_proportional(self, num_items: int) -> List[int]:
        res = []
        for _ in range(num_items):
            # TODO(szymon): should we ensure no repeats?
            mass = random.random() * self._it_sum.sum(0, len(self._storage))
            idx = self._it_sum.find_prefixsum_idx(mass)
            if len(self._storage) > num_items:
                while idx in res:  # ensure no repeats
                    mass = random.random() * self._it_sum.sum(0, len(self._storage))
                    idx = self._it_sum.find_prefixsum_idx(mass)
            res.append(idx)
        return res

    @DeveloperAPI
    @override(ReplayBuffer)
    def sample(self, num_items: int, beta: float) -> SampleBatchType:
        """Sample a batch of experiences and return priority weights, indices.

        Args:
            num_items (int): Number of items to sample from this buffer.
            beta (float): To what degree to use importance weights
                (0 - no corrections, 1 - full correction).

        Returns:
            SampleBatchType: Concatenated batch of items including "weights"
                and "batch_indexes" fields denoting IS of each sampled
                transition and original idxes in buffer of sampled experiences.
        """
        assert beta >= 0.0

        idxes = self._sample_proportional(num_items)

        weights = []
        batch_indexes = []
        p_min = self._it_min.min() / self._it_sum.sum()
        max_weight = (p_min * len(self._storage)) ** (-beta)

        for idx in idxes:
            p_sample = self._it_sum[idx] / self._it_sum.sum()
            weight = (p_sample * len(self._storage)) ** (-beta)
            count = self._storage[idx].count
            # If zero-padded, count will not be the actual batch size of the
            # data.
            if isinstance(self._storage[idx], SampleBatch) and \
                self._storage[idx].zero_padded:
                actual_size = self._storage[idx].max_seq_len
            else:
                actual_size = count
            weights.extend([weight / max_weight] * actual_size)
            batch_indexes.extend([idx] * actual_size)
            self._num_timesteps_sampled += count
        batch = self._encode_sample(idxes)

        # Note: prioritization is not supported in lockstep replay mode.
        if isinstance(batch, SampleBatch):
            batch["weights"] = np.array(weights)
            batch["batch_indexes"] = np.array(batch_indexes)

        return batch

    @DeveloperAPI
    def update_priorities(self, idxes: List[int],
                          priorities: List[float]) -> None:
        """Update priorities of sampled transitions.

        sets priority of transition at index idxes[i] in buffer
        to priorities[i].

        Parameters
        ----------
        idxes: [int]
          List of idxes of sampled transitions
        priorities: [float]
          List of updated priorities corresponding to
          transitions at the sampled idxes denoted by
          variable `idxes`.
        """
        # Making sure we don't pass in e.g. a torch tensor.
        assert isinstance(idxes, (list, np.ndarray)), \
            "ERROR: `idxes` is not a list or np.ndarray, but " \
            "{}!".format(type(idxes).__name__)
        assert len(idxes) == len(priorities)
        for idx, priority in zip(idxes, priorities):
            assert priority > 0
            assert 0 <= idx < len(self._storage)
            delta = priority ** self._alpha - self._it_sum[idx]
            self._prio_change_stats.push(delta)
            self._it_sum[idx] = priority ** self._alpha
            self._it_min[idx] = priority ** self._alpha

            self._max_priority = max(self._max_priority, priority)

    @DeveloperAPI
    @override(ReplayBuffer)
    def stats(self, debug: bool = False) -> Dict:
        parent = ReplayBuffer.stats(self, debug)
        if debug:
            parent.update(self._prio_change_stats.stats())
        return parent

    @DeveloperAPI
    @override(ReplayBuffer)
    def get_state(self) -> Dict[str, Any]:
        """Returns all local state.

        Returns:
            Dict[str, Any]: The serializable local state.
        """
        # Get parent state.
        state = super().get_state()
        # Add prio weights.
        state.update({
            "sum_segment_tree": self._it_sum.get_state(),
            "min_segment_tree": self._it_min.get_state(),
            "max_priority": self._max_priority,
        })
        return state

    @DeveloperAPI
    @override(ReplayBuffer)
    def set_state(self, state: Dict[str, Any]) -> None:
        """Restores all local state to the provided `state`.

        Args:
            state (Dict[str, Any]): The new state to set this buffer. Can be
                obtained by calling `self.get_state()`.
        """
        super().set_state(state)
        self._it_sum.set_state(state["sum_segment_tree"])
        self._it_min.set_state(state["min_segment_tree"])
        self._max_priority = state["max_priority"]


# Visible for testing.
_local_replay_buffer = None


class LocalReplayBuffer(ParallelIteratorWorker):
    """A replay buffer shard storing data for all policies (in multiagent setup).

    Ray actors are single-threaded, so for scalability, multiple replay actors
    may be created to increase parallelism."""

    def __init__(
        self,
        num_shards: int = 1,
        learning_starts: int = 1000,
        capacity: int = 10000,
        replay_batch_size: int = 1,
        prioritized_replay_alpha: float = 0.6,
        prioritized_replay_beta: float = 0.4,
        prioritized_replay_eps: float = 1e-6,
        replay_mode: str = "independent",
        replay_sequence_length: int = 1,
        replay_burn_in: int = 0,
        replay_zero_init_states: bool = True,
        buffer_size=DEPRECATED_VALUE,
    ):
        """Initializes a LocalReplayBuffer instance.

        Args:
            num_shards (int): The number of buffer shards that exist in total
                (including this one).
            learning_starts (int): Number of timesteps after which a call to
                `replay()` will yield samples (before that, `replay()` will
                return None).
            capacity (int): The capacity of the buffer. Note that when
                `replay_sequence_length` > 1, this is the number of sequences
                (not single timesteps) stored.
            replay_batch_size (int): The batch size to be sampled (in
                timesteps). Note that if `replay_sequence_length` > 1,
                `self.replay_batch_size` will be set to the number of
                sequences sampled (B).
            prioritized_replay_alpha (float): Alpha parameter for a prioritized
                replay buffer.
            prioritized_replay_beta (float): Beta parameter for a prioritized
                replay buffer.
            prioritized_replay_eps (float): Epsilon parameter for a prioritized
                replay buffer.
            replay_mode (str): One of "independent" or "lockstep". Determined,
                whether in the multiagent case, sampling is done across all
                agents/policies equally.
            replay_sequence_length (int): The sequence length (T) of a single
                sample. If > 1, we will sample B x T from this buffer.
            replay_burn_in (int): The burn-in length in case
                `replay_sequence_length` > 0. This is the number of timesteps
                each sequence overlaps with the previous one to generate a
                better internal state (=state after the burn-in), instead of
                starting from 0.0 each RNN rollout.
            replay_zero_init_states (bool): Whether the initial states in the
                buffer (if replay_sequence_length > 0) are alwayas 0.0 or
                should be updated with the previous train_batch state outputs.
        """
        # Deprecated args.
        if buffer_size != DEPRECATED_VALUE:
            deprecation_warning(
                "ReplayBuffer(size)", "ReplayBuffer(capacity)", error=False)
            capacity = buffer_size

        self.replay_starts = learning_starts // num_shards
        self.capacity = capacity // num_shards
        self.replay_batch_size = replay_batch_size
        self.prioritized_replay_beta = prioritized_replay_beta
        self.prioritized_replay_eps = prioritized_replay_eps
        self.replay_mode = replay_mode
        self.replay_sequence_length = replay_sequence_length
        self.replay_burn_in = replay_burn_in
        self.replay_zero_init_states = replay_zero_init_states

        if replay_sequence_length > 1:
            self.replay_batch_size = int(
                max(1, replay_batch_size // replay_sequence_length))
            logger.info(
                "Since replay_sequence_length={} and replay_batch_size={}, "
                "we will replay {} sequences at a time.".format(
                    replay_sequence_length, replay_batch_size,
                    self.replay_batch_size))

        if replay_mode not in ["lockstep", "independent"]:
            raise ValueError("Unsupported replay mode: {}".format(replay_mode))

        def gen_replay():
            while True:
                yield self.replay()

        ParallelIteratorWorker.__init__(self, gen_replay, False)

        def new_buffer():
            return PrioritizedReplayBuffer(
                self.capacity, alpha=prioritized_replay_alpha)

        self.replay_buffers = collections.defaultdict(new_buffer)

        # Metrics.
        self.add_batch_timer = TimerStat()
        self.replay_timer = TimerStat()
        self.update_priorities_timer = TimerStat()
        self.num_added = 0

        # Make externally accessible for testing.
        global _local_replay_buffer
        _local_replay_buffer = self
        # If set, return this instead of the usual data for testing.
        self._fake_batch = None

    @staticmethod
    def get_instance_for_testing():
        global _local_replay_buffer
        return _local_replay_buffer

    def get_host(self) -> str:
        return platform.node()

    def add_batch(self, batch: SampleBatchType) -> None:
        # Make a copy so the replay buffer doesn't pin plasma memory.
        batch = batch.copy()
        # Handle everything as if multiagent
        if isinstance(batch, SampleBatch):
            batch = MultiAgentBatch({DEFAULT_POLICY_ID: batch}, batch.count)

        with self.add_batch_timer:
            # Lockstep mode: Store under _ALL_POLICIES key (we will always
            # only sample from all policies at the same time).
            if self.replay_mode == "lockstep":
                # Note that prioritization is not supported in this mode.
                for s in batch.timeslices(self.replay_sequence_length):
                    self.replay_buffers[_ALL_POLICIES].add(s, weight=None)
            else:
                for policy_id, sample_batch in batch.policy_batches.items():
                    if self.replay_sequence_length == 1:
                        timeslices = sample_batch.timeslices(1)
                    else:
                        timeslices = timeslice_along_seq_lens_with_overlap(
                            sample_batch=sample_batch,
                            zero_pad_max_seq_len=self.replay_sequence_length,
                            pre_overlap=self.replay_burn_in,
                            zero_init_states=self.replay_zero_init_states,
                        )
                    for time_slice in timeslices:
                        # If SampleBatch has prio-replay weights, average
                        # over these to use as a weight for the entire
                        # sequence.
                        if "weights" in time_slice and \
                            len(time_slice["weights"]):
                            weight = np.mean(time_slice["weights"])
                        else:
                            weight = None
                        self.replay_buffers[policy_id].add(
                            time_slice, weight=weight)
        self.num_added += batch.count

    def replay(self) -> SampleBatchType:
        if self._fake_batch:
            fake_batch = SampleBatch(self._fake_batch)
            return MultiAgentBatch({
                DEFAULT_POLICY_ID: fake_batch
            }, fake_batch.count)

        if self.num_added < self.replay_starts:
            return None
        with self.replay_timer:
            # Lockstep mode: Sample from all policies at the same time an
            # equal amount of steps.
            if self.replay_mode == "lockstep":
                return self.replay_buffers[_ALL_POLICIES].sample(
                    self.replay_batch_size, beta=self.prioritized_replay_beta)
            else:
                samples = {}
                for policy_id, replay_buffer in self.replay_buffers.items():
                    samples[policy_id] = replay_buffer.sample(
                        self.replay_batch_size,
                        beta=self.prioritized_replay_beta)
                return MultiAgentBatch(samples, self.replay_batch_size)

    def update_priorities(self, prio_dict: Dict) -> None:
        with self.update_priorities_timer:
            for policy_id, (batch_indexes, td_errors) in prio_dict.items():
                new_priorities = (
                    np.abs(td_errors) + self.prioritized_replay_eps)
                self.replay_buffers[policy_id].update_priorities(
                    batch_indexes, new_priorities)

    def stats(self, debug: bool = False) -> Dict:
        stat = {
            "add_batch_time_ms": round(1000 * self.add_batch_timer.mean, 3),
            "replay_time_ms": round(1000 * self.replay_timer.mean, 3),
            "update_priorities_time_ms": round(
                1000 * self.update_priorities_timer.mean, 3),
        }
        for policy_id, replay_buffer in self.replay_buffers.items():
            stat.update({
                "policy_{}".format(policy_id): replay_buffer.stats(debug=debug)
            })
        return stat

    def get_state(self) -> Dict[str, Any]:
        state = {"num_added": self.num_added, "replay_buffers": {}}
        for policy_id, replay_buffer in self.replay_buffers.items():
            state["replay_buffers"][policy_id] = replay_buffer.get_state()
        return state

    def set_state(self, state: Dict[str, Any]) -> None:
        self.num_added = state["num_added"]
        buffer_states = state["replay_buffers"]
        for policy_id in buffer_states.keys():
            self.replay_buffers[policy_id].set_state(buffer_states[policy_id])


ReplayActor = ray.remote(num_cpus=0)(LocalReplayBuffer)
