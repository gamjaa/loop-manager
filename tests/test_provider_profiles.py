import configparser
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.profiles import profile_config, use_empty_provider_home
from tests.support.fixed_samples import PROVIDER_PROFILES
from loop_manager_internal.agents import agent_profiles


class ProviderProfilesTest(unittest.TestCase):
    def setUp(self):
        use_empty_provider_home(self)

    def test_role_resolves_model_alias(self):
        config = profile_config(self)

        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"test-strong": frozenset({"xhigh"})},
        ):
            self.assertEqual(loop_manager.role(config, "planner"), ("test-strong", "xhigh"))

    def test_worker_level_boundaries_and_cap(self):
        cases = [
            (complexity, risk, score + (risk == "critical"))
            for complexity, score in (("low", 1), ("medium", 3), ("high", 5))
            for risk in ("low", "medium", "high", "critical")
        ]

        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                plan = {"complexity": complexity, "risk": risk}
                self.assertEqual(loop_manager.worker_level(plan), expected)

    def test_selected_provider_requires_every_explicit_profile_and_alias(self):
        for provider in ("codex", "claude"):
            original = profile_config(self).read_text()
            for role_section, name in (
                (f"{provider}.roles", "planner"),
                (f"{provider}.roles", "planner_check"),
                *((f"{provider}.worker", f"level{i}") for i in range(1, 9)),
            ):
                for value in (None, "", "strong", ",high", "strong,", "strong,unsupported", "absent,high", "%"):
                    with self.subTest(provider=provider, name=name, value=value):
                        parser = configparser.ConfigParser(interpolation=None)
                        parser.read_string(original)
                        # No unselected provider settings are necessary.
                        for section in list(parser.sections()):
                            if not section.startswith(provider + "."):
                                parser.remove_section(section)
                        if value is None:
                            inherited = parser[role_section].pop(name)
                            parser["DEFAULT"][name] = inherited
                        else:
                            parser[role_section][name] = value
                        config = profile_config(self)
                        with config.open("w") as stream:
                            parser.write(stream)
                        catalog = {model: frozenset({"low", "medium", "high", "xhigh", "max"})
                                   for model in parser[f"{provider}.models"].values()}
                        with self.assertRaisesRegex(RuntimeError, name):
                            agent_profiles.validate_provider_profiles(config, provider, catalog)

    def test_explicit_alias_and_section_cannot_be_supplied_by_defaults(self):
        for provider in ("codex", "claude"):
            for change in ("missing-section", "missing-alias", "empty-model", "default-alias"):
                with self.subTest(provider=provider, change=change):
                    config = profile_config(self)
                    parser = configparser.ConfigParser(interpolation=None)
                    parser.read(config)
                    models = f"{provider}.models"
                    if change == "missing-section":
                        parser.remove_section(models)
                    elif change == "empty-model":
                        parser[models]["strong"] = ""
                    else:
                        model = parser[models].pop("strong")
                        if change == "default-alias":
                            parser["DEFAULT"]["strong"] = model
                    with config.open("w") as stream:
                        parser.write(stream)
                    with self.assertRaisesRegex(RuntimeError, models.replace(".", r"\.")):
                        agent_profiles.provider_profile(
                            config, provider, f"{provider}.roles", "planner", "Planner", {})

    def test_all_levels_can_share_supported_profile(self):
        for provider in ("codex", "claude"):
            config = profile_config(self)
            parser = configparser.ConfigParser()
            parser.read(config)
            for level in range(1, 9):
                parser[f"{provider}.worker"][f"level{level}"] = "balanced,high"
            with config.open("w") as stream:
                parser.write(stream)
            model = parser[f"{provider}.models"]["balanced"]
            for level in range(1, 9):
                self.assertEqual(agent_profiles.provider_worker_profile(
                    config, provider, level, {model: frozenset({"high"})}), (model, "high"))

    def test_fixed_profile_sample_is_complete_and_preserves_reviewer(self):
        config = PROVIDER_PROFILES
        catalogs = {
            "codex": {model: frozenset({"low", "medium", "high", "xhigh", "max"})
                      for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-terra", "gpt-6-luna")},
            "claude": {model: agent_profiles.CLAUDE_EFFORTS
                       for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-haiku-4-5-20251001")},
        }
        for provider, catalog in catalogs.items():
            agent_profiles.validate_provider_profiles(config, provider, catalog)
            model, effort = agent_profiles.provider_profile(
                config, provider, f"{provider}.roles", "reviewer", "reviewer", catalog)
            self.assertEqual((model, effort),
                             ("gpt-6.1-sol" if provider == "codex" else "claude-opus-5", "high"))
        for level, (model, effort) in enumerate((
            ("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5", "medium"),
            ("claude-sonnet-5-5", "medium"), ("claude-sonnet-5-5", "high"),
            ("claude-opus-5-5", "medium"), ("claude-opus-5-5", "high"),
            ("claude-opus-5-5", "xhigh"), ("claude-opus-5-5", "max")), 1):
            self.assertEqual(agent_profiles.provider_worker_profile(
                config, "claude", level, catalogs["claude"]), (model, effort))
    def test_worker_profile_resolves_level_alias(self):
        config = profile_config(self)

        model_efforts = {
            "test-fast": frozenset({"medium"}),
            "test-balanced": frozenset({"high"}),
            "test-strong": frozenset({"xhigh"}),
        }
        with mock.patch("loop_manager.codex_model_efforts", return_value=model_efforts):
            self.assertEqual(loop_manager.worker_profile(config, 1), ("test-fast", "medium"))
            self.assertEqual(loop_manager.worker_profile(config, 4), ("test-balanced", "high"))
            self.assertEqual(loop_manager.worker_profile(config, 7), ("test-strong", "xhigh"))

    def test_worker_profile_rejects_invalid_level(self):
        config = profile_config(self)

        for level in (0, 9, True):
            with self.subTest(level=level):
                with self.assertRaisesRegex(RuntimeError, "Worker level"):
                    loop_manager.worker_profile(config, level)

    def test_worker_profile_rejects_invalid_configuration(self):
        cases = [
            (
                "missing-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel2 = fast,medium\n",
                "level1 설정이 없습니다",
            ),
            (
                "empty-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 =\n",
                "Model alias/Effort 설정이 비어",
            ),
            (
                "malformed-profile",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast\n",
                "Model alias,Effort",
            ),
            (
                "empty-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = ,medium\n",
                "Model alias 설정이 비어",
            ),
            (
                "empty-effort",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,\n",
                "Effort 설정이 비어",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfast = test-strong\n[codex.worker]\nlevel1 = fast,minimal\n",
                "Model/Effort 조합을 Codex CLI가 지원하지 않습니다: test-strong,minimal",
            ),
            (
                "unsupported-model",
                "[codex.models]\nfast = unavailable\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model을 Codex CLI가 지원하지 않습니다: unavailable",
            ),
            (
                "missing-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = strong,high\n",
                "Model alias가 .* 없습니다",
            ),
            (
                "empty-model",
                "[codex.models]\nfast =\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model 설정이 비어",
            ),
            (
                "malformed-ini",
                "codex.models]\nfast = model-fast\n",
                "설정 파일 형식이 올바르지 않습니다",
            ),
            (
                "malformed-profile-interpolation",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,%\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.worker\] level1",
            ),
            (
                "malformed-model-interpolation",
                "[codex.models]\nfast = model-%\n[codex.worker]\nlevel1 = fast,medium\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.models\] fast",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            for name, contents, message in cases:
                with self.subTest(name=name):
                    config = Path(directory) / f"{name}.ini"
                    config.write_text(contents, encoding="utf-8")
                    with mock.patch(
                        "loop_manager.codex_model_efforts",
                        return_value={
                            "test-strong": frozenset(
                                {"low", "medium", "high", "xhigh", "max", "ultra"}
                            )
                        },
                    ):
                        with self.assertRaisesRegex(RuntimeError, message):
                            loop_manager.worker_profile(config, 1)

    def test_codex_model_efforts_reads_effective_catalog_for_worktree(self):
        catalog = {
            "models": [
                {
                    "slug": "test-reserve",
                    "supported_reasoning_levels": [
                        {"effort": "max"}, {"effort": "ultra"}
                    ],
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(catalog))
        worktree = Path("/project/worktree")
        with mock.patch("loop_manager.command", return_value=completed) as command:
            efforts = loop_manager.codex_model_efforts(worktree)

        self.assertEqual(efforts, {"test-reserve": frozenset({"max", "ultra"})})
        self.assertEqual(command.call_args.args[0], ["codex", "debug", "models"])
        self.assertEqual(command.call_args.kwargs["cwd"], worktree)

    def test_run_codex_validates_profile_in_worker_catalog(self):
        config = profile_config(self)
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"test-balanced": frozenset({"high"})},
            ) as model_efforts, mock.patch(
                "loop_manager._handoff_run", return_value=completed
            ):
                loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        model_efforts.assert_called_once_with(worktree)

    def test_profiles_validate_model_specific_efforts(self):
        model_efforts = {
            "test-strong": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
            "test-fast": frozenset({"low", "medium", "high", "xhigh", "max"}),
        }
        contents = (
            "[codex.models]\n"
            "strong = test-strong\n"
            "fast = test-fast\n"
            "[codex.roles]\n"
            "planner = strong,max\n"
            "reviewer = strong,ultra\n"
            "[codex.worker]\n"
            "level1 = fast,max\n"
            "level2 = strong,minimal\n"
            "level3 = fast,ultra\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text(contents, encoding="utf-8")
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value=model_efforts,
            ):
                self.assertEqual(loop_manager.role(config, "planner"), ("test-strong", "max"))
                self.assertEqual(loop_manager.role(config, "reviewer"), ("test-strong", "ultra"))
                self.assertEqual(loop_manager.worker_profile(config, 1), ("test-fast", "max"))
                for level in (2, 3):
                    with self.subTest(level=level):
                        with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                            loop_manager.worker_profile(config, level)

    def test_worker_codex_uses_level_model_and_effort(self):
        config = profile_config(self)
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"test-balanced": frozenset({"high"})},
            ), mock.patch("loop_manager._handoff_run", return_value=completed) as run:
                code, message = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        self.assertEqual((code, message), (0, ""))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-m") + 1], "test-balanced")
        self.assertIn('model_reasoning_effort="high"', arguments)

    def test_unsupported_worker_effort_fails_before_codex_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[codex.models]\nstrong = test-strong\n[codex.worker]\nlevel1 = strong,minimal\n",
                encoding="utf-8",
            )
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={
                    "test-strong": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
                },
            ), mock.patch("loop_manager._handoff_run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                    loop_manager.run_codex(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )
            run.assert_not_called()
