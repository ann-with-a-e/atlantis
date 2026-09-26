from django.test import override_settings
from django.test.runner import DiscoverRunner


class AtlantisTestRunner(DiscoverRunner):
	"""The default runner, with the real Airtable credentials taken away.

	settings.py loads .env unconditionally, so a test run would otherwise pick
	up the production PAT and base. Approving a T3 calls submit_ship inline, so
	any test that posts an approval creates a real record in the live base.
	Blanking the credentials here makes that impossible for every test, present
	and future; submit_ship treats an unconfigured Airtable as a failed
	submission rather than an error, so nothing needs to know about this.

	Tests that exercise the Airtable path set their own dummy credentials with
	override_settings, which still takes precedence over this.

	The bulletin channel goes the same way: every Slack-linked signup invites
	somebody to it, and a test login shouldn't reach the real workspace.
	"""

	def setup_test_environment(self, **kwargs):
		super().setup_test_environment(**kwargs)
		self._airtable_guard = override_settings(
			AIRTABLE_PAT="", AIRTABLE_BASE_ID="", AIRTABLE_TABLE_ID="",
			AIRTABLE_EMAILS_TABLE_ID="",
			SLACK_BULLETIN_CHANNEL_ID="",
		)
		self._airtable_guard.enable()

	def teardown_test_environment(self, **kwargs):
		self._airtable_guard.disable()
		super().teardown_test_environment(**kwargs)
