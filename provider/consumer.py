"""Consumer class.

/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
"""

import base64
import json
import logging
import os
import requests
import time

# HEADS UP! I'm importing confluent_kafka.Consumer as KafkaConsumer to avoid a
# naming conflict with my own Consumer class
from confluent_kafka import Consumer as KafkaConsumer, KafkaError, TopicPartition
from database import Database
from datetime import datetime
from datetimeutils import secondsSince
from multiprocessing import Process, Manager
from urlparse import urlparse

local_dev = os.getenv('LOCAL_DEV', 'False')
payload_limit = int(os.getenv('PAYLOAD_LIMIT', 900000))
check_ssl = (local_dev == 'False')

processingManager = Manager()


# Each Consumer instance will have a shared dictionary that will be used to
# indicate state, and desired state changes between this process, and the ConsumerProcess.
def newSharedDictionary():
    sharedDictionary = processingManager.dict()
    sharedDictionary['lastPoll'] = datetime.max
    return sharedDictionary

class Consumer:
    class State:
        Initializing = 'Initializing'
        Running = 'Running'
        Stopping = 'Stopping'
        Restart = 'Restart'
        Dead = 'Dead'
        Disabled = 'Disabled'

    def __init__(self, trigger, params):
        self.trigger = trigger
        self.params = params

        self.sharedDictionary = newSharedDictionary()

        self.process = ConsumerProcess(trigger, params, self.sharedDictionary)
        self.__restartCount = 0

    def currentState(self):
        return self.sharedDictionary['currentState']

    def desiredState(self):
        return self.sharedDictionary['desiredState']

    def setDesiredState(self, newState):
        self.sharedDictionary['desiredState'] = newState

    def shutdown(self):
        if self.currentState() == Consumer.State.Disabled:
            self.sharedDictionary['currentState'] = Consumer.State.Dead
            self.setDesiredState(Consumer.State.Dead)
        else:
            self.sharedDictionary['currentState'] = Consumer.State.Stopping
            self.setDesiredState(Consumer.State.Dead)

    def disable(self):
        self.setDesiredState(Consumer.State.Disabled)

    def start(self):
        self.process.start()

    # should only be called by the Doctor thread
    def restart(self):
        if self.desiredState() == Consumer.State.Dead:
            logging.info('[{}] Request to restart a consumer that is already slated for deletion.'.format(self.trigger))
            return

        self.__restartCount += 1

        logging.info('[{}] Quietly shutting down consumer for restart'.format(self.trigger))
        self.setDesiredState(Consumer.State.Restart)
        self.process.join()
        logging.info('Consumer has shut down')

        # user may have interleaved a request to delete the trigger, check again
        if self.desiredState() != Consumer.State.Dead:
            logging.info('[{}] Starting new consumer thread'.format(self.trigger))
            self.sharedDictionary = newSharedDictionary()
            self.process = ConsumerProcess(self.trigger, self.params, self.sharedDictionary)
            self.process.start()

    def restartCount(self):
        return self.__restartCount

    def lastPoll(self):
        return self.sharedDictionary['lastPoll']

    def secondsSinceLastPoll(self):
        return secondsSince(self.lastPoll())


class ConsumerProcess (Process):
    max_retries = 6    # Maximum number of times to retry firing trigger

    def __init__(self, trigger, params, sharedDictionary):
        Process.__init__(self)

        self.daemon = True

        self.trigger = trigger
        self.isMessageHub = params["isMessageHub"]
        self.triggerURL = self.__triggerURL(params["triggerURL"])
        self.brokers = params["brokers"]
        self.topic = params["topic"]

        self.sharedDictionary = sharedDictionary

        if 'status' in params and params['status']['active'] == False:
            self.sharedDictionary['currentState'] = Consumer.State.Disabled
            self.sharedDictionary['desiredState'] = Consumer.State.Disabled
        else:
            self.sharedDictionary['currentState'] = Consumer.State.Initializing
            self.sharedDictionary['desiredState'] = Consumer.State.Running

        if self.isMessageHub:
            self.username = params["username"]
            self.password = params["password"]

        # handle the case where there may be existing triggers that do not
        # have the isJSONData field set
        if "isJSONData" in params:
            self.encodeValueAsJSON = params["isJSONData"]
        else:
            self.encodeValueAsJSON = False

        if "isBinaryValue" in params:
            self.encodeValueAsBase64 = params["isBinaryValue"]
        else:
            self.encodeValueAsBase64 = False

        if "isBinaryKey" in params:
            self.encodeKeyAsBase64 = params["isBinaryKey"]
        else:
            self.encodeKeyAsBase64 = False

        if "wrapBase64Encoding" in params:
            self.wrapBase64 = params["wrapBase64Encoding"]
        else:
            self.wrapBase64 = True

        self.database = Database()

        # always init consumer to None in case the consumer needs to shut down
        # before the KafkaConsumer is fully initialized/assigned
        self.consumer = None

        # potentially squirrel away the message that would overflow the payload
        self.queuedMessage = None

    # this only records the current state, and does not affect a state transition
    def __recordState(self, newState):
        self.sharedDictionary['currentState'] = newState

    def currentState(self):
        return self.sharedDictionary['currentState']

    def setDesiredState(self, newState):
        logging.info('[{}] Request to set desiredState to {}'.format(self.trigger, newState))

        if self.sharedDictionary['desiredState'] == Consumer.State.Dead and newState != Consumer.State.Dead:
            logging.info('[{}] Asking to kill a consumer that is already marked for death. Doing nothing.'.format(self.trigger))
            return
        else:
            logging.info('[{}] Setting desiredState to: {}'.format(self.trigger, newState))
            self.sharedDictionary['desiredState'] = newState

    def desiredState(self):
        return self.sharedDictionary['desiredState']

    # convenience method for checking if desiredState is Running
    def __shouldRun(self):
        return self.desiredState() == Consumer.State.Running

    def lastPoll(self):
        return self.sharedDictionary['lastPoll']

    def updateLastPoll(self):
        self.sharedDictionary['lastPoll'] = datetime.now()

    def secondsSinceLastPoll(self):
        return secondsSince(self.lastPoll())

    def __triggerURL(self, originalURL):
        apiHost = os.getenv('API_HOST')

        if apiHost is not None:
            logging.info('[{}] Environment variable defined for API_HOST. Overriding host value defined for trigger in DB with {}'.format(self.trigger, apiHost))
            parsed = urlparse(originalURL)
            parts = parsed.netloc.split('@')
            auth = parts[0]
            newURL = parsed._replace(netloc='{}@{}'.format(auth, apiHost))

            return newURL.geturl()
        else:
            return originalURL

    def run(self):
        try:
            self.consumer = self.__createConsumer()

            while self.__shouldRun():
                messages = self.__pollForMessages()

                if len(messages) > 0:
                    self.__fireTrigger(messages)

                time.sleep(0.1)

            logging.info("[{}] Consumer exiting main loop".format(self.trigger))
        except Exception as e:
            logging.error('[{}] Uncaught exception: {}'.format(self.trigger, e))

        if self.desiredState() == Consumer.State.Dead:
            logging.info('[{}] Permanently killing consumer because desired state is Dead'.format(self.trigger))
        elif self.desiredState() == Consumer.State.Restart:
            logging.info('[{}] Quietly letting the consumer thread stop in order to allow restart.'.format(self.trigger))
            # nothing else to do because this Thread is about to go away
        elif self.desiredState() == Consumer.State.Disabled:
            logging.info('[{}] Quietly letting the consumer thread stop in order to disable the feed.'.format(self.trigger))
        else:
            # uh-oh... this really shouldn't happen
            logging.error('[{}] Consumer stopped without being asked'.format(self.trigger))

        try:
            if self.consumer is not None:
                logging.info('[{}] Cleaning up consumer'.format(self.trigger))
                logging.debug('[{}] Closing KafkaConsumer'.format(self.trigger))
                self.consumer.unsubscribe()
                self.consumer.close()
                logging.info('[{}] Successfully closed KafkaConsumer'.format(self.trigger))

                logging.debug('[{}] Dellocating KafkaConsumer'.format(self.trigger))
                self.consumer = None
                logging.info('[{}] Successfully cleaned up consumer'.format(self.trigger))
        except Exception as e:
            logging.error('[{}] Uncaught exception while shutting down consumer: {}'.format(self.trigger, e))
        finally:
            logging.info('[{}] Recording consumer as {}. Bye bye!'.format(self.trigger, self.desiredState()))
            self.__recordState(self.desiredState())

    def __createConsumer(self):
        if self.__shouldRun():
            config = {'metadata.broker.list': ','.join(self.brokers),
                        'group.id': self.trigger,
                        'default.topic.config': {'auto.offset.reset': 'latest'},
                        'enable.auto.commit': False,
                        'api.version.request': True
                    }

            if self.isMessageHub:
                # append Message Hub specific config
                config.update({'ssl.ca.location': '/etc/ssl/certs/',
                                'sasl.mechanisms': 'PLAIN',
                                'sasl.username': self.username,
                                'sasl.password': self.password,
                                'security.protocol': 'sasl_ssl'
                             })

            consumer = KafkaConsumer(config)
            consumer.subscribe([self.topic], self.__on_assign, self.__on_revoke)
            logging.info("[{}] Now listening in order to fire trigger".format(self.trigger))
            return consumer

    def __pollForMessages(self):
        messages = []
        totalPayloadSize = 0
        batchMessages = True

        if self.__shouldRun():
            while batchMessages and (self.secondsSinceLastPoll() < 2):
                if self.queuedMessage != None:
                    logging.debug('[{}] Handling message left over from last batch.'.format(self.trigger))
                    message = self.queuedMessage
                    self.queuedMessage = None
                else:
                    message = self.consumer.poll(1.0)

                if self.secondsSinceLastPoll() < 0:
                    logging.info('[{}] Completed first poll'.format(self.trigger))
                    self.__recordState(Consumer.State.Running)

                if (message is not None):
                    if not message.error():
                        logging.debug("Consumed message: {}".format(str(message)))
                        messagePayload = self.__getMessagePayload(message)
                        messageSize = self.__sizeMessagePayload(messagePayload)
                        if totalPayloadSize + messageSize > payload_limit:
                            if len(messages) == 0:
                                logging.error('[{}] Single message at offset {} exceeds payload size limit. Skipping this message!'.format(self.trigger, message.offset()))
                                self.consumer.commit(message=message, async=False)
                            else:
                                logging.debug('[{}] Message at offset {} would cause payload to exceed the size limit. Queueing up for the next round...'.format(self.trigger, message.offset()))
                                self.queuedMessage = message

                            # in any case, we need to stop batching now
                            batchMessages = False
                        else:
                            totalPayloadSize += messageSize
                            messages.append({'message': message, 'payload': messagePayload, 'size': messageSize})
                    elif message.error().code() != KafkaError._PARTITION_EOF:
                        logging.error('[{}] Error polling: {}'.format(self.trigger, message.error()))
                        batchMessages = False
                    else:
                        logging.debug('[{}] No more messages. Stopping batch op.'.format(self.trigger))
                        batchMessages = False
                else:
                    logging.debug('[{}] message was None. Stopping batch op.'.format(self.trigger))
                    batchMessages = False

        logging.debug('[{}] Completed poll'.format(self.trigger))

        if len(messages) > 0:
            logging.info("[{}] Found {} messages with a total size of {} bytes".format(self.trigger, len(messages), totalPayloadSize))

        self.updateLastPoll()
        return messages

    # decide whether or not to disable a trigger based on the status code returned
    # from firing the trigger. Specifically, disable on all 4xx status codes
    # except 408 (gateway timeout) and 429 (throttle)
    def __shouldDisable(self, status_code):
        return status_code in range(400, 500) and status_code not in [408, 429]

    # Fires a trigger and commits offset on success
    # messages is a list of dictionaries with the following format:
    #   [{message: "kafka-message-object", payload: "trigger-payload", size: "trigger-payload-size"}, ...]
    def __fireTrigger(self, messagesDictionary):
        if self.__shouldRun():
            # I'm sure there is a much more clever way to do this ;)
            mappedMessages = []
            messages = []
            for message in messagesDictionary:
                messages.append(message['message'])
                mappedMessages.append(message['payload'])

            lastMessage = messages[len(messages) - 1]
            payload = {}
            payload['messages'] = mappedMessages
            retry = True
            retry_count = 0

            logging.info("[{}] Firing trigger with {} messages".format(self.trigger,len(mappedMessages)))

            while retry:
                try:
                    response = requests.post(self.triggerURL, json=payload, timeout=10.0, verify=check_ssl)
                    status_code = response.status_code
                    logging.info("[{}] Repsonse status code {}".format(self.trigger, status_code))

                    # Manually commit offset if the trigger was fired successfully. Retry firing the trigger
                    # for a select set of status codes
                    if status_code in range(200, 300):
                        if status_code == 204:
                            logging.info("[{}] Successfully fired trigger".format(self.trigger))
                        else:
                            response_json = response.json()
                            if 'activationId' in response_json and response_json['activationId'] is not None:
                                logging.info("[{}] Fired trigger with activation {}".format(self.trigger, response_json['activationId']))
                            else:
                                logging.info("[{}] Successfully fired trigger".format(self.trigger))
                        # the consumer may have consumed messages that did not make it into the messages array.
                        # the consumer may have consumed messages that did not make it into the messages array.
                        # be sure to only commit to the messages that were actually fired.
                        self.consumer.commit(offsets=self.__getOffsetList(messages), async=False)
                        retry = False
                    elif self.__shouldDisable(status_code):
                        logging.error('[{}] Error talking to OpenWhisk, status code {}'.format(self.trigger, status_code))
                        response_dump = {
                            'request': {
                                'method': response.request.method,
                                'url': response.request.url,
                                'path_url': response.request.path_url,
                                'headers': response.request.headers,
                                'body': response.request.body
                            },
                            'response': {
                                'status_code': response.status_code,
                                'ok': response.ok,
                                'reason': response.reason,
                                'url': response.url,
                                'headers': response.headers,
                                'content': response.content
                            }
                        }

                        logging.error('[{}] Dumping the content of the request and response:\n{}'.format(self.trigger, response_dump))

                        # abandon all hope?
                        self.setDesiredState(Consumer.State.Disabled)
                        # mark it disabled in the DB
                        self.database.disableTrigger(self.trigger, status_code)
                        retry = False
                except requests.exceptions.RequestException as e:
                    logging.error('[{}] Error talking to OpenWhisk: {}'.format(self.trigger, e))

                if retry:
                    retry_count += 1

                    if retry_count <= self.max_retries:
                        sleepyTime = pow(2,retry_count)
                        logging.info("[{}] Retrying in {} second(s)".format(self.trigger, sleepyTime))
                        time.sleep(sleepyTime)
                    else:
                        logging.warn("[{}] Skipping {} messages to offset {} of partition {}".format(self.trigger, len(messages), lastMessage.offset(), lastMessage.partition()))
                        self.consumer.commit(offsets=self.__getOffsetList(messages), async=False)
                        retry = False

    # return the dict that will be sent as the trigger payload
    def __getMessagePayload(self, message):
        return {
            'value': self.__encodeMessageIfNeeded(message.value()),
            'topic': message.topic(),
            'partition': message.partition(),
            'offset': message.offset(),
            'key': self.__encodeKeyIfNeeded(message.key())
        }

    # return the size in bytes of the trigger payload for this message
    def __sizeMessagePayload(self, messagePayload):
        return len(json.dumps(messagePayload).encode('utf-8'))

    # return list of TopicPartition which represent the _next_ offset to consume
    def __getOffsetList(self, messages):
        offsets = []
        for message in messages:
            # Add one to the offset, otherwise we'll consume this message again.
            # That's just how Kafka works, you place the bookmark at the *next* message.
            offsets.append(TopicPartition(message.topic(), message.partition(), message.offset() + 1))

        return offsets

    def __base64Encode(self, value):
        if self.wrapBase64:
            # The encodestring method will wrap (insert a newline every 64 characters)
            # This is a legacy base64 encoding often not handled by newer decoding libraries
            return base64.encodestring(value).strip()
        else:
            # The b64encode method will not wrap encoded values
            return base64.b64encode(value).strip()

    def __encodeMessageIfNeeded(self, value):
        if self.encodeValueAsBase64:
            # For binary data we do not have to encode as unicode at all. The value should remain of type 'str' which
            # persists all 8 bits (0x-0xff) contrary to 'utf-8' which is a variable size encoding that only persists 7
            # bits with the highest order bit having a special meaning (continuation character).
            try:
                parsed = self.__base64Encode(value)
                logging.debug('[{}] Successfully encoded a binary message.'.format(self.trigger))
                return parsed
            except:
                logging.error('[{}] Unable to encode a binary message, sending message with empty payload'.format(self.trigger))
                return None

        # For text-based encodings let's make sure whatever data we're getting is utf-8 encoded.
        try:
            value = value.encode('utf-8')
        except UnicodeDecodeError:
            logging.warn('[{}] Value contains non-unicode bytes. Replacing invalid bytes.'.format(self.trigger))
            value = unicode(value, errors='replace').encode('utf-8')

        if self.encodeValueAsJSON:
            try:
                parsed = json.loads(value)
                logging.debug('[{}] Successfully encoded a message as JSON.'.format(self.trigger))
                return parsed
            except ValueError:
                # no big deal, just return the original value
                logging.warn('[{}] I was asked to encode a message as JSON, but I failed.'.format(self.trigger))
                value = "\"{}\"".format(value)
                pass

        logging.debug('[{}] Returning un-encoded message'.format(self.trigger))
        return value

    def __encodeKeyIfNeeded(self, key):
        if self.encodeKeyAsBase64 and key is not None:
            try:
                parsed = self.__base64Encode(key)
                logging.debug('[{}] Successfully encoded a binary key.'.format(self.trigger))
                return parsed
            except:
                logging.warn('[{}] Unable to encode a binary key.'.format(self.trigger))
                pass

        logging.debug('[{}] Returning un-encoded message'.format(self.trigger))
        return key

    def __on_assign(self, consumer, partitions):
        logging.info('[{}] Completed partition assignment. Connected to broker(s)'.format(self.trigger))

    def __on_revoke(self, consumer, partitions):
        logging.info('[{}] Partition assignment has been revoked. Disconnected from broker(s)'.format(self.trigger))
