import asyncio
import functools
import queue
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple, Union

from pika import BlockingConnection, ConnectionParameters, SelectConnection
from pika.adapters.asyncio_connection import AsyncioConnection
from pika.exceptions import ConnectionClosedByClient
from pika.exchange_type import ExchangeType


if TYPE_CHECKING:
    from pika import BasicProperties
    from pika.spec import Basic


def send_message(routing_key: str, message: str, custom_connection: Optional[Union[SelectConnection, BlockingConnection]] = None):
    if custom_connection is not None:
        connection = custom_connection
    else:
        connection = BlockingConnection()
    channel = connection.channel()
    exchange_name = routing_key.split('.')[0]
    channel.exchange_declare(exchange=exchange_name, exchange_type=ExchangeType.topic)

    channel.basic_publish(exchange=exchange_name,
                          routing_key=routing_key,
                          body=message)
    if custom_connection is None:
        connection.close()


def listen_to(
    exchange: str,
    listen_map: Dict[str, str],
    callback: Callable[[str, bytes, "Basic.Deliver", "BasicProperties"], Any],
    logging_interface_obj: Any = None
):
    """
    call consumer.stop() to stop listening.

    Arguments:
        exchange -- exchange name, generally, should be routing_key.split('.')[0]
        listen_map -- {queue_name: routing_key}
        callback -- callback function.
            args: routing_key, message, deliver, properties
    """

    consumer = WrappedConsumer(exchange, listen_map, callback, logging_interface_obj)
    th = threading.Thread(target=consumer.run)
    consumer.register_thread(th)
    th.start()
    return consumer


__connection = None


def get_substained_connection(**kwargs):
    global __connection
    if __connection is None:
        parameters = ConnectionParameters(heartbeat=60, **kwargs)
        __connection = BlockingConnection(parameters)
    return __connection


def close_substained_connection():
    global __connection
    if __connection is not None:
        __connection.close()
        __connection = None


class WrappedConsumer(object):
    """
    This class is taken from Pika's example code.

    This is an example consumer that will handle unexpected interactions
    with RabbitMQ such as channel and connection closures.

    If RabbitMQ closes the connection, this class will stop and indicate
    that reconnection is necessary. You should look at the output, as
    there are limited reasons why the connection may be closed, which
    usually are tied to permission related issues or socket timeouts.

    If the channel is closed, it will indicate a problem with one of the
    commands that were issued and that should surface in the output as well.

    """
    EXCHANGE_TYPE = ExchangeType.topic

    def __init__(
        self,
        exchange: str,
        bindings: Dict[str, str],
        callback: Callable[[str, bytes, "Basic.Deliver", "BasicProperties"], Any],
        logging_interface_obj=None
    ):
        """Create a new instance of the consumer class, passing in the AMQP
        URL used to connect to RabbitMQ.

        :param str amqp_url: The AMQP url to connect with

        """
        self.should_reconnect = False
        self.was_consuming = False

        self._connection = None
        self._channel = None
        self._closing = False
        self._consumer_tag: list = []
        self._consuming = False
        # In production, experiment with higher prefetch values
        # for higher consumer throughput
        self._prefetch_count = 1

        self.exchange = exchange
        self.bindings = bindings
        self._callback = callback
        if logging_interface_obj is None:
            import logging
            LOG_FORMAT = ('%(levelname) -1s %(asctime)s %(name) -1s %(funcName) '
                          '-35s %(lineno) -5d: %(message)s')
            logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
            self.logger: logging.Logger = logging.getLogger(__name__)
        else:
            self.logger = logging_interface_obj
        #
        self._thread: Optional[threading.Thread] = None

    def _do_callback(self, routing_key: str, message: bytes, deliver: "Basic.Deliver", properties: "BasicProperties") -> None:
        self._callback(routing_key, message, deliver, properties)

    def register_thread(self, th: threading.Thread) -> None:
        self._thread = th

    def connect(self):
        """This method connects to RabbitMQ, returning the connection handle.
        When the connection is established, the on_connection_open method
        will be invoked by pika.

        :rtype: pika.adapters.asyncio_connection.AsyncioConnection

        """
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        return AsyncioConnection(
            on_open_callback=self.on_connection_open,
            on_open_error_callback=self.on_connection_open_error,
            on_close_callback=self.on_connection_closed)

    def close_connection(self):
        self._consuming = False
        if self._connection.is_closing or self._connection.is_closed:
            self.logger.info('Connection is closing or already closed')
        else:
            self.logger.info('Closing connection')
            self._connection.close()

    def on_connection_open(self, _unused_connection):
        """This method is called by pika once the connection to RabbitMQ has
        been established. It passes the handle to the connection object in
        case we need it, but in this case, we'll just mark it unused.

        :param pika.adapters.asyncio_connection.AsyncioConnection _unused_connection:
           The connection

        """
        self.logger.info('Connection opened')
        self.open_channel()

    def on_connection_open_error(self, _unused_connection, err):
        """This method is called by pika if the connection to RabbitMQ
        can't be established.

        :param pika.adapters.asyncio_connection.AsyncioConnection _unused_connection:
           The connection
        :param Exception err: The error

        """
        self.logger.error('Connection open failed: %s', err)
        self.reconnect()

    def on_connection_closed(self, _unused_connection, reason):
        """This method is invoked by pika when the connection to RabbitMQ is
        closed unexpectedly. Since it is unexpected, we will reconnect to
        RabbitMQ if it disconnects.

        :param pika.connection.Connection connection: The closed connection obj
        :param Exception reason: exception representing reason for loss of
            connection.

        """
        self._channel = None
        if self._closing:
            self._connection.ioloop.stop()
        else:
            self.logger.warning('Connection closed, reconnect necessary: %s', reason)

            if not isinstance(reason, ConnectionClosedByClient):
                self.reconnect()

    def reconnect(self):
        """Will be invoked if the connection can't be opened or is
        closed. Indicates that a reconnect is necessary then stops the
        ioloop.

        """
        self.should_reconnect = True
        self.stop()

    def open_channel(self):
        """Open a new channel with RabbitMQ by issuing the Channel.Open RPC
        command. When RabbitMQ responds that the channel is open, the
        on_channel_open callback will be invoked by pika.

        """
        self.logger.info('Creating a new channel')
        self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        """This method is invoked by pika when the channel has been opened.
        The channel object is passed in so we can make use of it.

        Since the channel is now open, we'll declare the exchange to use.

        :param pika.channel.Channel channel: The channel object

        """
        self.logger.info('Channel opened')
        self._channel = channel
        self.add_on_channel_close_callback()
        self.setup_exchange(self.exchange)

    def add_on_channel_close_callback(self):
        """This method tells pika to call the on_channel_closed method if
        RabbitMQ unexpectedly closes the channel.

        """
        self.logger.info('Adding channel close callback')
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reason):
        """Invoked by pika when RabbitMQ unexpectedly closes the channel.
        Channels are usually closed if you attempt to do something that
        violates the protocol, such as re-declare an exchange or queue with
        different parameters. In this case, we'll close the connection
        to shutdown the object.

        :param pika.channel.Channel: The closed channel
        :param Exception reason: why the channel was closed

        """
        self.logger.warning('Channel %i was closed: %s', channel, reason)
        self.close_connection()

    def setup_exchange(self, exchange_name):
        """Setup the exchange on RabbitMQ by invoking the Exchange.Declare RPC
        command. When it is complete, the on_exchange_declareok method will
        be invoked by pika.

        :param str|unicode exchange_name: The name of the exchange to declare

        """
        self.logger.info('Declaring exchange: %s', exchange_name)
        # Note: using functools.partial is not required, it is demonstrating
        # how arbitrary data can be passed to the callback when it is called
        cb = functools.partial(
            self.on_exchange_declareok, userdata=exchange_name)
        self._channel.exchange_declare(
            exchange=exchange_name,
            exchange_type=self.EXCHANGE_TYPE,
            callback=cb)

    def on_exchange_declareok(self, _unused_frame, userdata):
        """Invoked by pika when RabbitMQ has finished the Exchange.Declare RPC
        command.

        :param pika.Frame.Method unused_frame: Exchange.DeclareOk response frame
        :param str|unicode userdata: Extra user data (exchange name)

        """
        self.logger.info('Exchange declared: %s', userdata)
        for key in self.bindings:
            self.setup_queue(key)

    def setup_queue(self, queue_name):
        """Setup the queue on RabbitMQ by invoking the Queue.Declare RPC
        command. When it is complete, the on_queue_declareok method will
        be invoked by pika.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        self.logger.info('Declaring queue %s', queue_name)
        cb = functools.partial(self.on_queue_declareok, userdata=queue_name)
        self._channel.queue_declare(queue=queue_name, callback=cb)

    def on_queue_declareok(self, _unused_frame, userdata):
        """Method invoked by pika when the Queue.Declare RPC call made in
        setup_queue has completed. In this method we will bind the queue
        and exchange together with the routing key by issuing the Queue.Bind
        RPC command. When this command is complete, the on_bindok method will
        be invoked by pika.

        :param pika.frame.Method _unused_frame: The Queue.DeclareOk frame
        :param str|unicode userdata: Extra user data (queue name)

        """
        queue_name = userdata
        routing_key = self.bindings[queue_name]
        self.logger.info('Binding %s to %s with %s', self.exchange, queue_name,
                         routing_key)
        cb = functools.partial(self.on_bindok, userdata=queue_name)
        self._channel.queue_bind(
            queue_name,
            self.exchange,
            routing_key=routing_key,
            callback=cb)

    def on_bindok(self, _unused_frame, userdata):
        """Invoked by pika when the Queue.Bind method has completed. At this
        point we will set the prefetch count for the channel.

        :param pika.frame.Method _unused_frame: The Queue.BindOk response frame
        :param str|unicode userdata: Extra user data (queue name)

        """
        self.logger.info('Queue bound: %s', userdata)
        self.set_qos()

    def set_qos(self):
        """This method sets up the consumer prefetch to only be delivered
        one message at a time. The consumer must acknowledge this message
        before RabbitMQ will deliver another one. You should experiment
        with different prefetch values to achieve desired performance.

        """
        self._channel.basic_qos(
            prefetch_count=self._prefetch_count, callback=self.on_basic_qos_ok)

    def on_basic_qos_ok(self, _unused_frame):
        """Invoked by pika when the Basic.QoS method has completed. At this
        point we will start consuming messages by calling start_consuming
        which will invoke the needed RPC commands to start the process.

        :param pika.frame.Method _unused_frame: The Basic.QosOk response frame

        """
        self.logger.info('QOS set to: %d', self._prefetch_count)
        self.start_consuming()

    def start_consuming(self):
        """This method sets up the consumer by first calling
        add_on_cancel_callback so that the object is notified if RabbitMQ
        cancels the consumer. It then issues the Basic.Consume RPC command
        which returns the consumer tag that is used to uniquely identify the
        consumer with RabbitMQ. We keep the value to use it when we want to
        cancel consuming. The on_message method is passed in as a callback pika
        will invoke when a message is fully received.

        """
        self.logger.info('Issuing consumer related RPC commands')
        self.add_on_cancel_callback()
        for key in self.bindings:
            self._consumer_tag.append(self._channel.basic_consume(key, self.on_message))
        self.was_consuming = True
        self._consuming = True

    def add_on_cancel_callback(self):
        """Add a callback that will be invoked if RabbitMQ cancels the consumer
        for some reason. If RabbitMQ does cancel the consumer,
        on_consumer_cancelled will be invoked by pika.

        """
        self.logger.info('Adding consumer cancellation callback')
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        """Invoked by pika when RabbitMQ sends a Basic.Cancel for a consumer
        receiving messages.

        :param pika.frame.Method method_frame: The Basic.Cancel frame

        """
        self.logger.info('Consumer was cancelled remotely, shutting down: %r',
                         method_frame)
        if self._channel:
            self._channel.close()

    def on_message(self, _unused_channel, basic_deliver, properties, body):
        """Invoked by pika when a message is delivered from RabbitMQ. The
        channel is passed for your convenience. The basic_deliver object that
        is passed in carries the exchange, routing key, delivery tag and
        a redelivered flag for the message. The properties passed in is an
        instance of BasicProperties with the message properties and the body
        is the message that was sent.

        :param pika.channel.Channel _unused_channel: The channel object
        :param pika.Spec.Basic.Deliver: basic_deliver method
        :param pika.Spec.BasicProperties: properties
        :param bytes body: The message body

        """
        self._do_callback(basic_deliver.routing_key, body, basic_deliver, properties)
        self.logger.info('Received message # %s from %s: %s',
                         basic_deliver.delivery_tag, properties.app_id, body)
        self.acknowledge_message(basic_deliver.delivery_tag)

    def acknowledge_message(self, delivery_tag):
        """Acknowledge the message delivery from RabbitMQ by sending a
        Basic.Ack RPC method for the delivery tag.

        :param int delivery_tag: The delivery tag from the Basic.Deliver frame

        """
        self.logger.info('Acknowledging message %s', delivery_tag)
        self._channel.basic_ack(delivery_tag)

    def stop_consuming(self):
        """Tell RabbitMQ that you would like to stop consuming by sending the
        Basic.Cancel RPC command.

        """
        if self._channel:
            self.logger.info('Sending a Basic.Cancel RPC command to RabbitMQ')
            for tag in self._consumer_tag:
                cb = functools.partial(
                    self.on_cancelok, userdata=tag)
                self._channel.basic_cancel(tag, cb)

    def on_cancelok(self, _unused_frame, userdata):
        """This method is invoked by pika when RabbitMQ acknowledges the
        cancellation of a consumer. At this point we will close the channel.
        This will invoke the on_channel_closed method once the channel has been
        closed, which will in-turn close the connection.

        :param pika.frame.Method _unused_frame: The Basic.CancelOk frame
        :param str|unicode userdata: Extra user data (consumer tag)

        """
        self._consuming = False
        self.logger.info(
            'RabbitMQ acknowledged the cancellation of the consumer: %s',
            userdata)
        self.close_channel()

    def close_channel(self):
        """Call to close the channel with RabbitMQ cleanly by issuing the
        Channel.Close RPC command.

        """
        self.logger.info('Closing the channel')
        self._channel.close()

    def run(self):
        """Run the example consumer by connecting to RabbitMQ and then
        starting the IOLoop to block and allow the AsyncioConnection to operate.

        """
        self._connection = self.connect()
        self._connection.ioloop.run_forever()

    def stop(self):
        """Cleanly shutdown the connection to RabbitMQ by stopping the consumer
        with RabbitMQ. When RabbitMQ confirms the cancellation, on_cancelok
        will be invoked by pika, which will then closing the channel and
        connection. The IOLoop is started again because this method is invoked
        when CTRL-C is pressed raising a KeyboardInterrupt exception. This
        exception stops the IOLoop which needs to be running for pika to
        communicate with RabbitMQ. All of the commands issued prior to starting
        the IOLoop will be buffered but not processed.

        """
        if not self._closing:
            self._closing = True
            self.logger.info('Stopping')
            if self._consuming:
                self.stop_consuming()
            else:
                self._connection.ioloop.stop()
            if self._thread is not None:
                self._thread.join()
                self._thread = None
            self.logger.info('Stopped')


class NoLogInterface(object):
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class PikaMessageQueue(object):
    def __init__(self) -> None:
        self._queue: queue.Queue[Tuple[str, str]] = queue.Queue()
        self._stopped = False

    def push(self, routing_key: str, message: str) -> None:
        self._queue.put((routing_key, message))

    def run(self) -> None:
        while not self._stopped:
            try:
                routing_key, message = self._queue.get(block=True, timeout=0.5)
            except queue.Empty:
                continue  # maybe false awakened by self.stop()
            if not self._stopped:
                send_message(routing_key, message, get_substained_connection())
        with self._queue.not_full:
            self._queue.not_full.notify()  # wake the caller thread of self.stop()

    def stop(self) -> None:
        with self._queue.not_full:
            self._stopped = True
            self._queue.not_empty.notify()  # wake the runner thread
            self._queue.not_full.wait(1.)  # wait for the runner thread to stop gracefully
        close_substained_connection()  # safe to close the connection now
