import os
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from slack_sdk.errors import SlackApiError

from ..models import AuditLog
from ..views import helpers
from ..views.helpers import invite_to_autojoin_channels, invite_to_channel
from .base import BaseTestCase, User, grant_perms, make_user, message_texts
from .test_auth import SLACK_USER_RESPONSE, TOKEN, USERINFO

BULLETIN = "C0BULLETIN"
CHANNELS = ["C0BULLETIN", "C0ATLANTIS", "C0HELP", "C0CHECKPOINT"]


def run_inline(target):
	target()


def slack_error(**data):
	response = MagicMock()
	response.get.side_effect = data.get
	return SlackApiError("invite failed", response)


@patch.object(helpers.slack_client, "conversations_invite")
class InviteToChannelTests(TestCase):
	def test_invites_every_id_once_and_skips_blanks(self, invite):
		invite.return_value = {"ok": True}
		self.assertEqual(invite_to_channel(BULLETIN, ["U1", "", "U2", "U1"]), 2)
		invite.assert_called_once_with(channel=BULLETIN, users=["U1", "U2"], force=True)

	def test_batches_at_the_slack_limit(self, invite):
		invite.return_value = {"ok": True}
		ids = [f"U{i}" for i in range(helpers.SLACK_INVITE_BATCH + 5)]
		self.assertEqual(invite_to_channel(BULLETIN, ids), len(ids))
		self.assertEqual(invite.call_count, 2)
		self.assertEqual(len(invite.call_args_list[1].kwargs["users"]), 5)

	def test_per_user_errors_are_not_counted(self, invite):
		invite.return_value = {"ok": True, "errors": [{"user": "U1", "error": "already_in_channel"}]}
		self.assertEqual(invite_to_channel(BULLETIN, ["U1", "U2"]), 1)

	def test_a_failed_batch_does_not_stop_the_next(self, invite):
		invite.side_effect = [slack_error(error="already_in_channel"), {"ok": True}]
		with patch.object(helpers, "SLACK_INVITE_BATCH", 1):
			self.assertEqual(invite_to_channel(BULLETIN, ["U1", "U2"]), 1)

	def test_no_channel_does_nothing(self, invite):
		self.assertEqual(invite_to_channel("", ["U1"]), 0)
		invite.assert_not_called()


@override_settings(SLACK_AUTOJOIN_CHANNEL_IDS=CHANNELS)
class InviteToAutojoinChannelsTests(TestCase):
	def test_invites_to_every_configured_channel(self):
		with patch.object(helpers, "invite_to_channel", return_value=1) as invite:
			total = invite_to_autojoin_channels(["U1", "U2"])
		self.assertEqual(invite.call_count, len(CHANNELS))
		for channel in CHANNELS:
			invite.assert_any_call(channel, ["U1", "U2"])
		self.assertEqual(total, len(CHANNELS))

	def test_one_channel_rejecting_the_batch_does_not_stop_the_others(self):
		# Realistic failure: Slack itself rejects one channel's invite (bad
		# scope, wrong channel) — invite_to_channel already swallows that, so
		# the other three channels still get invited.
		with patch.object(helpers.slack_client, "conversations_invite") as invite:
			invite.side_effect = [slack_error(error="channel_not_found"), {"ok": True}, {"ok": True}, {"ok": True}]
			total = invite_to_autojoin_channels(["U1"])
		self.assertEqual(invite.call_count, len(CHANNELS))
		self.assertEqual(total, 3)

	@override_settings(SLACK_AUTOJOIN_CHANNEL_IDS=[])
	def test_unconfigured_does_nothing(self):
		with patch.object(helpers, "invite_to_channel") as invite:
			self.assertEqual(invite_to_autojoin_channels(["U1"]), 0)
		invite.assert_not_called()


@override_settings(SLACK_AUTOJOIN_CHANNEL_IDS=CHANNELS)
@patch.object(helpers, "_start_thread", run_inline)
class InviteToChannelsViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")

	def test_non_organizer_cannot_invite(self):
		self.client.force_login(make_user("pleb"))
		with patch.object(helpers, "invite_to_autojoin_channels") as invite:
			self.client.post(reverse("invite_to_channels"))
		invite.assert_not_called()

	def test_get_not_allowed(self):
		self.client.force_login(self.organizer)
		self.assertEqual(self.client.get(reverse("invite_to_channels")).status_code, 405)

	def test_invites_every_slack_linked_user(self):
		make_user("ada", slack_id="U0ADA")
		make_user("nobody", slack_id="")
		self.client.force_login(self.organizer)

		with patch.object(helpers, "invite_to_autojoin_channels", return_value=8) as invite:
			response = self.client.post(reverse("invite_to_channels"))

		self.assertRedirects(response, reverse("users"), fetch_redirect_response=False)
		self.assertEqual(invite.call_args.args[0], ["U0ORG", "U0ADA"])
		self.assertTrue(AuditLog.objects.filter(action="invite_to_channels").exists())

	@override_settings(SLACK_AUTOJOIN_CHANNEL_IDS=[])
	def test_unconfigured_reports_and_invites_nobody(self):
		self.client.force_login(self.organizer)
		with patch.object(helpers, "invite_to_autojoin_channels") as invite:
			response = self.client.post(reverse("invite_to_channels"), follow=True)
		invite.assert_not_called()
		self.assertTrue(any("SLACK_AUTOJOIN_CHANNEL_IDS" in m for m in message_texts(response)))


@patch.dict(os.environ, {"DEFAULT_PFP": "https://example.com/default.png"})
@patch.object(helpers, "_start_thread", run_inline)
@patch("atlantis_site.views.client.auth.slack_client.users_info", return_value=SLACK_USER_RESPONSE)
@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_access_token")
class SignupAutojoinTests(TestCase):
	def _callback(self, mock_token, **userinfo):
		mock_token.return_value = {"userinfo": {**USERINFO, **userinfo}, **TOKEN}
		return self.client.get(reverse("auth_callback"))

	def test_new_signup_is_invited(self, mock_token, _slack):
		with patch.object(helpers, "invite_to_autojoin_channels") as invite:
			self._callback(mock_token)
		invite.assert_called_once_with(["U0SLACK"])

	def test_returning_user_is_not_reinvited(self, mock_token, _slack):
		User.objects.create_user(username="user_abc123", email="tester@example.com")
		with patch.object(helpers, "invite_to_autojoin_channels") as invite:
			self._callback(mock_token)
		invite.assert_not_called()

	def test_signup_without_slack_is_not_invited(self, mock_token, _slack):
		with patch.object(helpers, "invite_to_autojoin_channels") as invite:
			self._callback(mock_token, slack_id="")
		invite.assert_not_called()

	def test_invite_failure_does_not_block_login(self, mock_token, _slack):
		with patch.object(helpers, "invite_to_autojoin_channels", side_effect=RuntimeError("boom")):
			response = self._callback(mock_token)
		self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
