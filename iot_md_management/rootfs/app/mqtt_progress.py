"""Optional TLS MQTT observer: bounded/coalesced, authenticated, never commands."""
import hashlib
import hmac
import json
import ssl
import threading
import time
from operation_reporting import DEFAULT_TOPIC, FIELDS, report_bytes, report_topic, topic_template
from update_operation_contract import reduce_operation

MAX_REPORT_BYTES = 4096
MAX_PENDING_REPORTS = 512


def tracking_settings(options):
    method = options.get('update_tracking_method', 'api')
    if method not in ('api', 'mqtt'):
        raise ValueError('Update tracking method must be api or mqtt')
    result = {'method': method}
    if method == 'api':
        return result  # API mode neither imports MQTT nor validates unused credentials.
    host, port = options.get('mqtt_host', 'mqtt-broker.home.arpa'), options.get('mqtt_port', 8883)
    if (not isinstance(host, str) or not 1 <= len(host) <= 253 or
            any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:' for c in host) or
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
        raise ValueError('MQTT broker settings are invalid')
    cert, key = options.get('mqtt_client_cert_path') or '', options.get('mqtt_client_key_path') or ''
    if bool(cert) != bool(key):
        raise ValueError('MQTT client certificate and private key must be configured together')
    result.update(host=host.lower().rstrip('.'), port=port,
        topic_template=topic_template(options.get('mqtt_progress_topic', DEFAULT_TOPIC)),
        username=str(options.get('mqtt_username') or ''), password=str(options.get('mqtt_password') or ''),
        ca_path=str(options.get('mqtt_ca_path') or ''), cert_path=cert, key_path=key,
        stale_s=max(10, min(120, int(options.get('mqtt_report_stale_s', 15)))),
        snapshot_interval_s=max(15, min(300, int(options.get('mqtt_snapshot_interval_s', 60)))))
    return result


class MQTTProgressReceiver:
    def __init__(self, store, controller, options, client_factory=None, now=time.monotonic):
        self.store, self.controller = store, controller
        self.settings = tracking_settings(options)
        self.client_factory = client_factory
        self.now = now
        self.client = None
        self.connected = False
        self.pending = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.counts = {'accepted': 0, 'rejected': 0, 'coalesced': 0, 'dropped': 0, 'applied': 0}
        self.state = 'disabled' if self.settings['method'] == 'api' else 'waiting'

    def _reject(self):
        with self.lock:
            self.counts['rejected'] += 1
        return False

    def receive(self, topic, payload):
        """Network callback performs bounded validation, not device I/O or writes."""
        if self.settings['method'] != 'mqtt' or not isinstance(topic, str) or len(topic) > 256:
            return self._reject()
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_REPORT_BYTES:
            return self._reject()
        try:
            report = json.loads(payload)
            if not isinstance(report, dict) or set(report) != set(FIELDS) | {'mac'}:
                return self._reject()
            identifier, mac = report['id'], report['mac']
            if (not isinstance(identifier, str) or len(identifier) != 32 or
                    any(c not in '0123456789abcdef' for c in identifier) or
                    not isinstance(mac, str) or len(mac) != 64 or any(c not in '0123456789abcdef' for c in mac)):
                return self._reject()
            secret = self.store.get_update_report_key(identifier)
            if not secret or topic != secret['topic']:
                return self._reject()
            expected = hmac.new(bytes.fromhex(secret['key']), report_bytes(report), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, mac):
                return self._reject()
            deployment = self.store.get_deployment(secret['deployment_id'])
            result = (deployment or {}).get('results', {}).get(secret['device_id'], {})
            device = self.store.get_device(secret['device_id'])
            if (not device or not device.get('enabled') or not deployment or
                    deployment['status'] not in ('queued', 'active') or
                    result.get('status') in ('complete', 'failed', 'cancelled', 'unresolved')):
                return self._reject()
            dispatch = result.get('dispatch') or {}
            if dispatch.get('tracking_method') != 'mqtt':
                return self._reject()
            if report_topic(self.settings['topic_template'], dispatch['device_id'], identifier) != topic:
                return self._reject()
            projection = reduce_operation(report, dispatch, dispatch['device_id'], deployment['update'], result.get('update_operation'))
            if projection is None:
                return self._reject()  # Duplicate/replayed reports cannot refresh freshness.
            with self.lock:
                old = self.pending.get(identifier)
                if old:
                    order = lambda value: (value['revision'], value['boot_epoch'], value['sample_sequence'])
                    if order(report) <= order(old['report']):
                        self.counts['rejected'] += 1
                        return False
                    self.counts['coalesced'] += 1
                elif len(self.pending) >= MAX_PENDING_REPORTS:
                    self.counts['dropped'] += 1
                    return False
                self.pending[identifier] = {'report': report, 'deployment': secret['deployment_id'],
                    'device': secret['device_id'], 'received_at': self.now()}
                self.counts['accepted'] += 1
            return True
        except (ValueError, KeyError, TypeError, UnicodeError):
            return self._reject()

    def drain(self):
        with self.lock:
            values, self.pending = list(self.pending.values()), {}
        for item in values:
            try:
                # Do not call an aged queued report a newly responsive device.
                if self.now() - item['received_at'] > self.settings['stale_s']:
                    self._reject()
                    continue
                if self.controller.observe_mqtt_operation(item['deployment'], item['device'], item['report']):
                    with self.lock:
                        self.counts['applied'] += 1
            except Exception:
                self._reject()  # Never log response bodies, keys or credentials.

    def snapshot(self):
        with self.lock:
            return {'method': self.settings['method'], 'state': self.state,
                    'connected': self.connected, 'pending': len(self.pending), **self.counts}

    def _on_connect(self, client, userdata, flags, reason, properties):
        self.connected = reason == 0
        self.state = 'connected' if self.connected else 'retrying'
        if self.connected:
            subscription = self.settings['topic_template'].replace('{device_id}', '+').replace('{operation_id}', '+')
            client.subscribe(subscription, qos=1)

    def _on_disconnect(self, client, userdata, flags, reason, properties):
        self.connected = False
        self.state = 'retrying'

    def _on_message(self, client, userdata, message):
        try:
            self.receive(message.topic, bytes(message.payload))
        except Exception:
            self._reject()

    def _worker(self):
        while not self.stopped.wait(.2):
            self.drain()

    def start(self):
        if self.settings['method'] == 'api':
            return False
        try:
            import paho.mqtt.client as mqtt
            from paho.mqtt.properties import Properties
            from paho.mqtt.packettypes import PacketTypes
            context = ssl.create_default_context(cafile=self.settings['ca_path'] or None)
            if self.settings['cert_path']:
                context.load_cert_chain(self.settings['cert_path'], self.settings['key_path'])
            factory = self.client_factory or mqtt.Client
            self.client = factory(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
            self.client.tls_set_context(context)
            self.client.username_pw_set(self.settings['username'] or None, self.settings['password'] or None)
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.reconnect_delay_set(1, 60)
            self.client.connect_timeout = 5
            properties = Properties(PacketTypes.CONNECT)
            properties.MaximumPacketSize = 8192
            properties.ReceiveMaximum = 32
            self.client.connect_async(self.settings['host'], self.settings['port'], keepalive=30, properties=properties)
            self.client.loop_start()
            threading.Thread(target=self._worker, name='mqtt-progress', daemon=True).start()
            self.state = 'connecting'
            return True
        except Exception:
            self.state = 'unavailable'
            self.connected = False
            if self.client:
                try:
                    self.client.disconnect()
                    self.client.loop_stop()
                except Exception:
                    pass
            return False  # API reconciliation remains available on any startup failure.

    def stop(self):
        self.stopped.set()
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()
        self.connected = False
