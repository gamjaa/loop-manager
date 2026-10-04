"""가짜 관측과 프로세스 identity로 자원 계산·예약 안전성을 확인한다."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.verification import resources
from loop_manager_internal.verification.coordination import VerifyCoordinator
from loop_manager_internal.verification.resources import MIB, ResourceSampler, Snapshot, decide, parse_parallelism


class ResourceDecisionTests(unittest.TestCase):
    def snapshot(self, **values):
        return Snapshot(**{"at": 10, "cpus": 8, "busy": 1, "available": 4096 * MIB, "source": "fake", **values})

    def test_actual_load_and_pending_reservations_decide_without_fixed_division(self):
        low = self.snapshot()
        self.assertTrue(decide(low, now=10, running=2, cap="auto", global_running=100).granted)
        self.assertFalse(decide(self.snapshot(busy=7.8), now=10, running=2, cap="auto").granted)
        self.assertFalse(decide(low, now=10, running=2, cap="auto", pending_cpu=7).granted)
        self.assertEqual(decide(low, now=10, running=2, cap="auto"), decide(low, now=10, running=2, cap="auto"))

    def test_cpu_quota_user_cap_and_serial_memory_constraints(self):
        self.assertEqual(decide(self.snapshot(cpus=1.5, busy=0), now=10, running=2, cap=10).limit, 2)
        self.assertFalse(decide(self.snapshot(), now=10, running=1, cap=1).granted)
        self.assertFalse(decide(self.snapshot(available=32 * MIB), now=10, running=0, cap=1).granted)
        self.assertTrue(decide(self.snapshot(available=80 * MIB), now=10, running=0, cap=1, existing=True).granted)

    def test_unknown_or_stale_observation_serializes_and_recovers(self):
        for snapshot in (self.snapshot(busy=None), self.snapshot(at=0), self.snapshot(at=11), self.snapshot(available=None)):
            decision = decide(snapshot, now=10, running=0, cap="auto")
            self.assertTrue(decision.granted)
            self.assertEqual(decision.limit, 1)
            self.assertTrue(decision.fallback)
            self.assertFalse(decide(snapshot, now=10, running=0, cap="auto", global_running=1).granted)
        self.assertTrue(decide(self.snapshot(), now=10, running=2, cap="auto").granted)

    def test_pending_memory_and_positive_setting_validation(self):
        self.assertFalse(decide(self.snapshot(available=512 * MIB), now=10, running=0, cap="auto",
                                pending_memory=400 * MIB).granted)
        self.assertEqual([parse_parallelism(value) for value in ("auto", "1", "12")], ["auto", 1, 12])
        for value in ("0", "-1", "1.5", "AUTO", "", "1_0", "１２"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_parallelism(value)

    def test_sampler_measures_deltas_and_does_not_treat_first_sample_as_idle(self):
        clock = [0.0]
        samples = iter([(4, 100, 20, 1024 * MIB, 1.0, 4), (4, 200, 70, 1024 * MIB, 1.5, 4)])
        sampler = ResourceSampler(clock=lambda: clock[0], read=lambda: next(samples))
        self.assertIsNone(sampler.sample().busy)
        clock[0] = 1
        self.assertEqual(sampler.sample().busy, 2.0)
        with mock.patch.object(sampler, "read", side_effect=OSError("unavailable")):
            clock[0] = 2
            self.assertIsNone(sampler.sample().busy)
            self.assertEqual(sampler.sample().available, 1024 * MIB)

    def test_linux_nested_cgroup_quota_and_memory_are_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, cg = root / "proc", root / "cgroup"
            (proc / "self").mkdir(parents=True)
            leaf = cg / "tenant/leaf"
            leaf.mkdir(parents=True)
            (proc / "self/cgroup").write_text("0::/tenant/leaf\n")
            (proc / "self/mountinfo").write_text(f"1 0 0:1 / {cg.as_posix()} rw - cgroup2 cgroup rw\n")
            (proc / "stat").write_text("".join(f"cpu{number} 10 0 0 90 0 0 0 0\n" for number in range(4)))
            (proc / "meminfo").write_text("MemAvailable: 2097152 kB\n")
            (leaf / "cpu.max").write_text("max 100000")
            (leaf.parent / "cpu.max").write_text("200000 100000")
            (leaf / "cpu.stat").write_text("usage_usec 1000000\n")
            (leaf.parent / "cpu.stat").write_text("usage_usec 4000000\n")
            (leaf / "memory.max").write_text(str(512 * MIB))
            (leaf / "memory.current").write_text(str(480 * MIB))
            with mock.patch.object(resources.os, "sched_getaffinity", return_value=set(range(4)), create=True):
                cpus, _total, _busy, available, usage, _host = resources.linux_sample(proc)
            self.assertEqual((cpus, available), (2, 32 * MIB))
            self.assertEqual(usage, 4)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.identities = {100: 1, 200: 2}
        self.capacity = 8
        sampler = SimpleNamespace(sample=lambda: Snapshot(self.now, self.capacity, 0, 4096 * MIB, "fake"))
        self.coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: self.now,
                                             identity=lambda pid: self.identities.get(pid))
        self.a, self.sa = self.coordinator.open("auto")
        self.b, self.sb = self.coordinator.open("auto")

    def request(self, operation, **values):
        return self.coordinator.request(self.a, self.sa, operation, **values)

    def test_fresh_sample_after_request_clock_allows_parallel_reservations(self):
        def sample():
            self.now += 0.25
            return Snapshot(self.now, self.capacity, 0, 4096 * MIB, "fake")

        self.coordinator.sampler = SimpleNamespace(sample=sample)
        first = self.request("acquire")
        self.assertTrue(first["granted"])
        self.assertFalse(first["fallback"])
        reservation = self.coordinator.reservations[first["token"]]
        self.assertEqual((reservation["at"], reservation["born_at"]), (self.now, self.now))
        second = self.request("acquire")
        self.assertTrue(second["granted"])
        self.assertFalse(second["fallback"])
        self.assertEqual(len(self.coordinator.reservations), 2)

    def test_sample_that_ages_during_observation_serializes_then_recovers(self):
        def sample():
            snapshot = Snapshot(self.now, self.capacity, 0, 4096 * MIB, "fake")
            self.now += resources.SNAPSHOT_MAX_AGE + 0.25
            return snapshot

        self.coordinator.sampler = SimpleNamespace(sample=sample)
        first = self.request("acquire")
        self.assertTrue(first["granted"])
        self.assertTrue(first["fallback"])
        self.assertEqual(first["limit"], 1)
        self.assertFalse(self.request("acquire")["granted"])
        self.assertFalse(self.coordinator.request(self.b, self.sb, "acquire")["granted"])
        self.request("not_started", token=first["token"])
        self.coordinator.sampler = SimpleNamespace(
            sample=lambda: Snapshot(self.now, self.capacity, 0, 4096 * MIB, "fake"))
        recovered = self.coordinator.request(self.b, self.sb, "acquire")
        self.assertTrue(recovered["granted"])
        self.assertFalse(recovered["fallback"])

    def test_simultaneous_start_does_not_double_reserve_one_cpu(self):
        self.capacity = 1
        barrier, replies = threading.Barrier(2), []
        def reserve(session, secret):
            barrier.wait()
            replies.append(self.coordinator.request(session, secret, "acquire"))
        threads = [threading.Thread(target=reserve, args=values) for values in ((self.a, self.sa), (self.b, self.sb))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(reply["granted"] for reply in replies), 1)

    def test_idle_preserves_live_reservation_and_release_requires_exit_identity(self):
        token = self.request("acquire")["token"]
        self.request("bind", token=token, pid=100)
        self.request("idle", token=token)
        self.assertIn(token, self.coordinator.reservations)
        self.assertTrue(self.request("acquire", token=token)["granted"])
        with self.assertRaisesRegex(RuntimeError, "종료"):
            self.request("release", token=token)
        self.identities.pop(100)
        self.request("release", token=token)
        self.assertNotIn(token, self.coordinator.reservations)

    def test_unstarted_and_uncertain_reservations_do_not_expire_as_free_resources(self):
        self.capacity = 1
        token = self.request("acquire")["token"]
        self.now += 100
        self.assertFalse(self.coordinator.request(self.b, self.sb, "acquire")["granted"])
        self.coordinator.finish(self.a, stopped=False)
        self.now += 100
        self.assertFalse(self.coordinator.request(self.b, self.sb, "acquire")["granted"])
        self.assertTrue(self.coordinator.reservations[token]["uncertain"])
        self.coordinator.finish(self.a, stopped=True)
        self.assertTrue(self.coordinator.request(self.b, self.sb, "acquire")["granted"])

    def test_start_failure_other_session_and_authentication_are_isolated(self):
        token = self.request("acquire")["token"]
        with self.assertRaises(RuntimeError):
            self.coordinator.request(self.b, self.sb, "not_started", token=token)
        with self.assertRaisesRegex(RuntimeError, "인증"):
            self.coordinator.request(self.a, "incorrect", "not_started", token=token)
        self.request("not_started", token=token)
        self.assertFalse(self.coordinator.reservations)

    def test_waiting_verify_gets_turn_and_other_reservations_survive_finish(self):
        self.capacity = 1
        token = self.request("acquire")["token"]
        self.request("bind", token=token, pid=100)
        self.assertFalse(self.coordinator.request(self.b, self.sb, "acquire")["granted"])
        self.capacity = 8
        self.assertFalse(self.request("acquire")["granted"])
        other = self.coordinator.request(self.b, self.sb, "acquire")["token"]
        self.coordinator.finish(self.a, stopped=True)
        self.assertIn(other, self.coordinator.reservations)
        self.assertNotIn(token, self.coordinator.reservations)

    def test_bind_rejects_foreign_process_and_live_start_failure_release(self):
        token = self.request("acquire")["token"]
        self.coordinator.guard(self.a, lambda pid: pid == 100)
        with self.assertRaisesRegex(RuntimeError, "자식"):
            self.request("bind", token=token, pid=200)
        self.request("bind", token=token, pid=100)
        with self.assertRaisesRegex(RuntimeError, "이미 시작"):
            self.request("not_started", token=token)
