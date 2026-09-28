import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError
from telegram.ext import Application
from telegram.ext._utils.networkloop import network_retry_loop

os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123:test')
import mrcga_bot as bot


class PollingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_timeout_reaches_recovery_and_stops_application(self):
        request = bot.PollingRequest(connection_pool_size=1)
        stopped = asyncio.Event()
        context = SimpleNamespace(
            error=None,
            application=SimpleNamespace(stop_running=stopped.set),
        )
        errors = []
        handlers = []

        def on_error(error):
            errors.append(error)
            context.error = error
            handlers.append(asyncio.create_task(bot.handle_error(None, context)))

        bot._NETWORK_SHUTDOWN_REQUESTED = False
        try:
            with patch.object(request._client, 'request', AsyncMock(side_effect=httpx.PoolTimeout('pool occupied'))) as send:
                with self.assertLogs(bot.LOGGER, level='ERROR'):
                    await asyncio.wait_for(network_retry_loop(
                        action_cb=lambda: request.do_request('https://example.invalid', 'POST'),
                        on_err_cb=on_error,
                        description='test polling', interval=0,
                        is_running=lambda: not stopped.is_set(),
                        max_retries=-1, repeat_on_success=True,
                    ), timeout=3)
                    await asyncio.gather(*handlers)
                self.assertTrue(stopped.is_set())
                self.assertEqual(send.await_count, 1)
                self.assertEqual(type(errors[0]), NetworkError)
                self.assertIsInstance(errors[0].__cause__.__cause__, httpx.PoolTimeout)
        finally:
            bot._NETWORK_SHUTDOWN_REQUESTED = False
            await request.shutdown()

    async def test_overall_deadline_cancels_stuck_request(self):
        request = bot.PollingRequest()
        async def stuck(*args, **kwargs):
            await asyncio.Event().wait()
        try:
            with patch.object(bot.PollingRequest, 'REQUEST_DEADLINE', 0.02):
                with patch.object(request._client, 'request', stuck):
                    with self.assertRaises(NetworkError) as caught:
                        await request.do_request('https://example.invalid', 'POST')
            self.assertEqual(type(caught.exception), NetworkError)
            self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        finally:
            await request.shutdown()

    async def test_success_and_external_cancellation(self):
        request = bot.PollingRequest()
        try:
            with patch.object(request._client, 'request', AsyncMock(return_value=httpx.Response(200, content=b'{}'))):
                self.assertEqual(await request.do_request('https://example.invalid', 'POST'), (200, b'{}'))
            with patch.object(request._client, 'request', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await request.do_request('https://example.invalid', 'POST')
        finally:
            await request.shutdown()

    async def test_proxy_configuration_builds(self):
        request = bot.PollingRequest(connection_pool_size=1, proxy='http://127.0.0.1:7890')
        app = (Application.builder().token('123:test').get_updates_request(request)
               .proxy('http://127.0.0.1:7890').build())
        self.assertIs(app.bot._request[0], request)
        await request.shutdown()
        await app.bot.request.shutdown()


if __name__ == '__main__':
    unittest.main()
