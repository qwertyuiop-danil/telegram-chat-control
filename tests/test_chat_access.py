from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from telegram_control import accounts, cli, client
from telethon import types, functions
from telethon.errors import InviteHashExpiredError


class ChatAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        client.load_telethon()
        self.channel = types.Channel(id=7, title='Private', photo=types.ChatPhotoEmpty(), date=None, broadcast=True)

    async def test_invites_only_check_and_never_join(self):
        for link in ('https://t.me/+Ab_c-12', 'https://t.me/joinchat/Ab_c-12', 'tg://join?invite=Ab_c-12', 't.me/+Ab_c-12'):
            async def request(req):
                self.assertIsInstance(req, functions.messages.CheckChatInviteRequest)
                self.assertEqual(req.hash, 'Ab_c-12')
                return types.ChatInviteAlready(self.channel)
            current = AsyncMock(side_effect=request)
            self.assertIs(await client.resolve(current, link), self.channel)
            self.assertEqual(current.await_count, 1)
            current.get_entity.assert_not_called()
        for result in (types.ChatInvitePeek(self.channel, None), NS(request_needed=True), NS()):
            current = AsyncMock(return_value=result)
            with self.assertRaisesRegex(RuntimeError, 'forbidden'):
                await client.resolve(current, 'https://t.me/+Ab_c-12')
            self.assertEqual(current.await_count, 1)
            current.get_entity.assert_not_called()
        current = AsyncMock(side_effect=InviteHashExpiredError(None))
        with self.assertRaises(InviteHashExpiredError):
            await client.resolve(current, 'https://t.me/+Expired')
        self.assertEqual(current.await_count, 1)

    async def test_private_post_link_uses_current_membership(self):
        current = AsyncMock()
        async def dialogs():
            yield NS(entity=self.channel)
        current.iter_dialogs = dialogs
        self.assertIs(await client.resolve(current, 'https://t.me/c/7/81'), self.channel)
        current.get_entity.assert_not_called()
        current.assert_not_called()
        with self.assertRaises(RuntimeError):
            await client.resolve(current, 'https://t.me/c/8/81')
        self.channel.left = True
        with self.assertRaises(RuntimeError):
            await client.resolve(current, 'https://t.me/c/7/81')

    async def test_numeric_chat_id_without_entity_cache(self):
        current = AsyncMock()
        current.get_entity.side_effect = ValueError("Missing input entity")
        async def dialogs():
            yield NS(entity=self.channel)
        current.iter_dialogs = dialogs
        self.assertIs(await client.resolve(current, -1000000000007), self.channel)
        with self.assertRaises(ValueError):
            await client.resolve(current, -1000000000008)
        current.assert_not_called()

    async def test_live_open_sender_pagination_and_no_index_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            accounts.add(root, 100, 'Owner', '')
            user = types.User(id=9, first_name='Alice', username='alice')
            current = AsyncMock()
            current.__aenter__.return_value = current
            current.get_me.return_value = types.User(id=100)
            current.get_entity.return_value = user
            async def request(req):
                if isinstance(req, functions.messages.CheckChatInviteRequest):
                    return types.ChatInviteAlready(self.channel)
                self.assertIsInstance(req, functions.channels.GetFullChannelRequest)
                return NS(full_chat=NS(about='Description'))
            current.side_effect = request
            async def messages(entity, **kwargs):
                self.assertIs(entity, self.channel)
                self.assertEqual(kwargs, {'limit': 2, 'offset_id': 20, 'from_user': user})
                for mid in (19, 18):
                    message = types.Message(id=mid, peer_id=types.PeerChannel(7), from_id=types.PeerUser(9), date=None, message='hello')
                    message._sender = user
                    yield message
            current.iter_messages = messages
            args = cli.parser().parse_args(['chat', 'open', '--peer', 'https://t.me/+Abcd', '--sender', '@alice', '--limit', '1', '--cursor', '20'])
            with patch.object(client, 'telegram_client', return_value=current), patch.object(cli, 'ROOT', root):
                data = await cli.dispatch(args)
            self.assertEqual(data['meta']['next_cursor'], '19')
            self.assertEqual(data['meta']['chat']['about'], 'Description')
            self.assertEqual(data['items'][0]['sender']['id'], 9)
            self.assertEqual(data['items'][0]['chat']['chat_id'], -1000000000007)


if __name__ == '__main__':
    unittest.main()
