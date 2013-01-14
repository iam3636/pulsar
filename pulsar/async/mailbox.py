import sys
import logging
import tempfile

from pulsar import MailboxError, CouldNotParse, CommandNotFound, platform
from pulsar.utils.pep import to_bytes, ispy3k, pickle, ispy3k, set_event_loop

from .defer import log_failure, is_failure
from .access import get_actor, set_actor
from .servers import create_server
from .protocols import Protocol, ChunkResponse


__all__ = ['mailbox', 'ActorMessage']


LOGGER = logging.getLogger('pulsar.mailbox')


def mailbox(actor):
    '''Creates a :class:`Mailbox` for *actor*.'''
    if actor.is_monitor():
        return MonitorMailbox(actor)
    else:
        if platform.type == 'posix':
            address = 'unix:%s.pulsar' % actor.aid
        else:   #pragma    nocover
            address = ('127.0.0.1', 0)
        server = actor.requestloop.create_server(address=address,
                                            protocol_factory=MailboxProtocol,
                                            close_event_loop=True)
        server.event_loop.call_soon(send_mailbox_address, actor)
        return server

def actorid(actor):
    return actor.aid if hasattr(actor, 'aid') else actor


class ActorMessage(object):
    '''A message which travels from :class:`Actor` to
:class:`Actor` to perform a specific *command*. :class:`ActorMessage`
are not directly initialised using the constructor, instead they are
created by :meth:`ActorProxy.send` method.

.. attribute:: sender

    id of the actor sending the message.

.. attribute:: receiver

    id of the actor receiving the message.

.. attribute:: command

    command to be performed

.. attribute:: args

    Positional arguments in the message body

.. attribute:: kwargs

    Optional arguments in the message body
'''
    def __init__(self, command, sender=None, receiver=None,
                 args=None, kwargs=None):
        self.command = command
        self.sender = actorid(sender)
        self.receiver = actorid(receiver)
        self.args = args if args is not None else ()
        self.kwargs = kwargs if kwargs is not None else {}

    @classmethod
    def decode(cls, buffer):
        separator = b'\r\n'
        if buffer[0] != 42:
            raise CouldNotParse()
        idx = buffer.find(separator)
        if idx < 0:
            return None, buffer
        length = int(buffer[1:idx])
        idx += len(separator)
        total_length = idx + length
        if len(buffer) >= total_length:
            data, buffer = buffer[idx:total_length:], buffer[total_length:]
            if not ispy3k:
                data = bytes(data)
            args = pickle.loads(data)
            return cls(*args), buffer
        else:
            return None, buffer

    def encode(self):
        data = (self.command, self.sender, self.receiver,
                self.args, self.kwargs)
        bdata = pickle.dumps(data, protocol=2)
        return ('*%s\r\n' % len(bdata)).encode('utf-8') + bdata

    def __repr__(self):
        return self.command


class MailboxResponse(ChunkResponse):
    
    def decode(self, data):
        # The receiver could be different from the mail box actor. For
        # example a monitor uses the same mailbox as the arbiter
        message, data = ActorMessage.decode(data)
        return message
        
    def response(self, message):
        self.message = message
        actor = get_actor()
        receiver = actor.get_actor(message.receiver) or actor
        sender = receiver.get_actor(message.sender)
        command = receiver.command(message.command)
        try:
            if not command:
                raise CommandNotFound(message.command)
            args = message.args
            # If this is an internal command add the sender information
            if command.internal:
                args = (sender,) + args
            result = command(self, receiver, *args, **message.kwargs)
        except:
            result = sys.exc_info()
        result = make_async(result).add_both(self.encode)
        self.write(result)
        self._finished = True
            
    def encode(self, result):
        log_failure(result)
        if command.ack:
            # Send back the result as an ActorMessage
            if is_failure(result):
                m = ActorMessage('errback', sender=receiver, args=(result,))
            else:
                m = ActorMessage('callback', sender=receiver, args=(result,))
            return m.encode()
        else:
            return b''

    def finished(self):
        return bool(self.message)


class MailboxProtocol(Protocol):
    response_factory = MailboxResponse
    authenticated = False
    
    
def send_mailbox_address(actor):
    actor.logger.info('%s started at address %s', actor, actor.mailbox)
    a = get_actor()
    if a is not actor:
        set_actor(actor)
    if not actor.is_arbiter():
        return actor.send(actor.monitor, 'mailbox_address', actor.address)\
                    .add_callback(actor.link_actor)


class MonitorMailbox(object):
    '''A :class:`Mailbox` for a :class:`Monitor`. This is a proxy for the
arbiter inbox.'''
    active_connections = 0
    def __init__(self, actor):
        self.mailbox = actor.arbiter.mailbox

    @property
    def address(self):
        return self.mailbox.address

    @property
    def event_loop(self):
        return self.mailbox.event_loop

    def _run(self):
        pass
    
    def close(self):
        pass

