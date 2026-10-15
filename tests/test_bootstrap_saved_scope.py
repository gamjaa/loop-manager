from tests.support.bootstrap_saved_scope import BootstrapSavedScopeFixture


class BootstrapSavedScopeResumeTests(BootstrapSavedScopeFixture):
    def test_remote_bootstrap_child_resumes_saved_scope_without_identity_injection(self):
        self._assert_saved_scope_resume()
