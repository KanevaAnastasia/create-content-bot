import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app import syntax_telethon_fallback as syntax


def test_navigation_waits_for_each_menu_before_next_command(monkeypatch):
    events = []
    class Client:
        async def send_message(self, target, text):
            events.append(('send', text))
            return SimpleNamespace(id=len(events))
    async def wait(client, target, **kwargs):
        events.append(('wait', kwargs['expected_button']))
        return True
    monkeypatch.setattr(syntax, '_wait_for_syntax_menu_reply', wait)
    asyncio.run(syntax._send_syntax_video_preflight(Client(), None, provider='veo', model='veo3_fast'))
    assert events == [('send','/video'),('wait','Veo'),('send','Veo'),('wait','Сгенерировать'),('send','Сгенерировать'),('wait','configure')]


def test_unresponsive_menu_retries_only_menu_and_stops_before_media(monkeypatch):
    client = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(id=1),SimpleNamespace(id=2)]))
    wait=AsyncMock(return_value=False)
    monkeypatch.setattr(syntax, '_wait_for_syntax_menu_reply', wait)
    with pytest.raises(syntax.SyntaxVeoNotSubmittedError, match='menu acknowledgement'):
        asyncio.run(syntax._send_syntax_video_preflight(client,None,provider='veo',model='veo3_fast'))
    assert [c.args[1] for c in client.send_message.call_args_list] == ['/video','/video']
    assert [c.kwargs['after_id'] for c in wait.call_args_list] == [1,1]


def test_menu_wait_ignores_old_and_outgoing_messages(monkeypatch):
    clock=[0.0]
    monkeypatch.setattr(syntax.time,'monotonic',lambda:clock[0])
    async def sleep(seconds):clock[0]+=seconds
    monkeypatch.setattr(syntax.asyncio,'sleep',sleep)
    def msg(mid,out=False):return SimpleNamespace(id=mid,out=out,buttons=[[SimpleNamespace(text='Veo')]])
    class Client:
        async def get_messages(self,*args,**kwargs):
            return [msg(9),msg(12,True)] + ([msg(14)] if clock[0]>=40 else [])
    assert asyncio.run(syntax._wait_for_syntax_menu_reply(Client(),None,after_id=10,expected_button='Veo'))
    assert clock[0]==40
