from tests.support.bootstrap_saved_scope import BootstrapSavedScopeFixture


class BootstrapSavedScopeResumeTests(BootstrapSavedScopeFixture):
    def test_selected_runtime_b_survives_external_base_c_before_child_prevalidation(self):
        self._assert_saved_scope_resume(advance_after_selection=True)
