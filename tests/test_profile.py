from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from telegram_control import accounts, cli, client
from telethon import functions, types
from telethon.errors import RPCError


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        client.load_telethon()
        self.user = types.User(id=42, first_name='Person', username='person')

    async def test_common_groups_pages_and_empty(self):
        chat = types.Chat(id=8, title='Shared', photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1)
        current = AsyncMock(side_effect=[types.messages.Chats([chat]), types.messages.Chats([])])
        first = await client._profile_section(current, self.user, 'common-groups', 1, None)
        self.assertEqual(first['common_groups'][0]['id'], -8)
        self.assertEqual(first['pagination']['next_cursor'], '8')
        final = await client._profile_section(current, self.user, 'common-groups', 1, '8')
        self.assertFalse(final['pagination']['has_more'])
        self.assertEqual(current.call_args.args[0].max_id, 8)
        empty = await client._profile_section(AsyncMock(return_value=types.messages.Chats([])), self.user, 'common-groups', 20, None)
        self.assertEqual(empty['common_groups'], [])

    async def test_gifts_senders_dedications_and_cursor(self):
        original = types.StarGiftAttributeOriginalDetails(types.PeerUser(42), None, types.PeerUser(9), types.TextWithEntities('Original', []))
        gift = NS(id=1, title='Gift', slug='Gift-1', attributes=[original])
        saved = types.SavedStarGift(None, gift, from_id=types.PeerUser(9), message=types.TextWithEntities('Hello', []))
        sender = types.User(id=9, first_name='Sender', username='sender')
        current = AsyncMock(return_value=NS(users=[sender], chats=[], gifts=[saved], count=2, next_offset='next'))
        data = await client._profile_section(current, self.user, 'gifts', 20, 'previous')
        item = data['gifts'][0]
        self.assertEqual(item['sender']['link'], 'https://t.me/sender')
        self.assertEqual(item['message'], 'Hello')
        self.assertEqual(item['original_details']['message'], 'Original')
        self.assertEqual(data['pagination']['next_cursor'], 'next')
        self.assertEqual(current.call_args.args[0].offset, 'previous')
        self.assertTrue(current.call_args.args[0].exclude_unsaved)
        saved.name_hidden = True
        item = client._saved_gift(saved, {9: sender})
        self.assertIsNone(item['sender'])
        self.assertIsNone(item['original_details']['sender'])
        self.assertEqual(client._gift_sender(types.PeerUser(10), {})['link'], 'tg://user?id=10')
        with patch.object(functions.payments, 'GetSavedStarGiftsRequest', None):
            data = await client._profile_section(current, self.user, 'gifts', 20, None)
        self.assertIn('details_unavailable', data)

    async def test_profile_photo_cache_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            accounts.add(root, 100, 'Owner', '')
            channel = types.Channel(id=7, title='Blog', photo=types.ChatPhotoEmpty(), date=None, username='blog')
            detailed = NS(full_user=NS(about='Biography', common_chats_count=1, personal_channel_id=7), chats=[channel])
            current = AsyncMock()
            current.__aenter__.return_value = current
            current.get_entity.return_value = self.user
            current.side_effect = [detailed, RPCError(None, 'DENIED', 403)]
            current.download_profile_photo.return_value = '/tmp/avatar.jpg'
            with patch.object(client, 'telegram_client', return_value=current):
                data = await client.profile(root, 42, section='gifts', photo=True)
                self.assertEqual(data['profile']['about'], 'Biography')
                self.assertEqual(data['profile']['personal_channel']['link'], 'https://t.me/blog')
                self.assertEqual(data['profile']['photo_path'], '/tmp/avatar.jpg')
                self.assertIn('gifts_unavailable', data)
                cached = await client.profile(root, 42)
                self.assertEqual(cached['source'], 'cache')
                self.assertNotIn('gifts', cached)
                with self.assertRaises(ValueError):
                    await client.profile(root, 42, cursor='bad')

    async def test_cli_exposes_pagination(self):
        args = cli.parser().parse_args(['profile', 'show', '--peer', '42', '--section', 'common-groups', '--cursor', '8'])
        with patch.object(cli, 'profile', AsyncMock(return_value={'profile': {}, 'common_groups': [], 'pagination': {'has_more': True, 'next_cursor': '7'}})) as fetch:
            result = await cli.dispatch(args)
        self.assertEqual(result['meta']['next_cursor'], '7')
        self.assertEqual(fetch.call_args.kwargs['cursor'], '8')

class PersonalChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_details_posts_and_absence(self):
        client.load_telethon()
        channel = types.Channel(id=7, title='Blog', photo=types.ChatPhotoEmpty(), date=None, username='blog')
        detailed = NS(full_user=NS(personal_channel_id=7), chats=[channel])
        current = AsyncMock(return_value=NS(full_chat=NS(about='Channel description', participants_count=12)))
        async def messages(*args, **kwargs):
            self.assertEqual(kwargs['offset_id'], 9)
            yield types.Message(id=8, peer_id=types.PeerChannel(7), date=None, message='a' * 501, post=True)
        current.iter_messages = messages
        data = await client._personal_channel(current, detailed, 1, '9')
        self.assertEqual(data['personal_channel']['about'], 'Channel description')
        self.assertEqual(data['posts'][0]['link'], 'https://t.me/blog/8')
        self.assertTrue(data['posts'][0]['text_truncated'])
        self.assertEqual(data['posts'][0]['direction'], 'channel_post')
        self.assertEqual(data['pagination']['next_cursor'], '8')
        data = await client._personal_channel(current, NS(full_user=NS(personal_channel_id=None)), 20, None)
        self.assertIsNone(data['personal_channel'])
        self.assertEqual(data['posts'], [])
        self.assertIn('personal_channel_unavailable', await client._personal_channel(current, None, 20, None))
        args = cli.parser().parse_args(['profile', 'show', '--peer', '42', '--section', 'personal-channel'])
        self.assertEqual(args.section, 'personal-channel')


if __name__ == '__main__':
    unittest.main()
