"""Kafka message producer.

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
import logging
import sys
import traceback

from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable, KafkaTimeoutError, AuthenticationFailedError
from kafka.version import __version__
from random import shuffle


logging.basicConfig(stream=sys.stdout, level=logging.INFO,
        format='%(levelname)-8s %(asctime)s %(message)s',
        datefmt='[%H:%M:%S]')

max_cached_producers = 10

def main(params):
    producer = None
    logging.info("Using kafka-python %s", str(__version__))

    print("Validating parameters")
    validationResult = validateParams(params)
    if validationResult[0] != True:
        return {'error': validationResult[1]}
    else:
        validatedParams = validationResult[1]

    attempt = 0
    max_attempts = 3

    result = {"success": True}

    while attempt < max_attempts:
        attempt += 1
        print("Starting attempt {}".format(attempt))

        try:
            print("Getting producer")
            producer = getProducer(validatedParams)

            topic = validatedParams['topic']
            print("Finding topic {}".format(topic))
            partition_info = producer.partitions_for(topic)
            print("Found topic {} with partition(s) {}".format(topic, partition_info))

            break
        except Exception as e:
            if attempt == max_attempts:
                producer = None
                logging.warning(e)
                traceback.print_stack(limit=5)
                result = getResultForException(e)

    # we successfully connected and found the topic metadata... let's send!
    if producer is not None:
        try:
            print("Producing message")

            # only use the key parameter if it is present
            value = validatedParams['value']
            if 'key' in validatedParams:
                messageKey = validatedParams['key']
                future = producer.send(
                    topic, bytes(value, 'utf-8'), key=bytes(messageKey, 'utf-8'))
            else:
                future = producer.send(topic, bytes(value, 'utf-8'))

            sent = future.get(timeout=20)
            msg = "Successfully sent message to {}:{} at offset {}".format(
                sent.topic, sent.partition, sent.offset)
            print(msg)
            result = {"success": True, "message": msg}
        except Exception as e:
            logging.warning(e)
            traceback.print_stack(limit=5)
            result = getResultForException(e)

    return result

def getResultForException(e):
    if isinstance(e, KafkaTimeoutError):
        return {'error': 'Timed out communicating with Message Hub'}
    elif isinstance(e, AuthenticationFailedError):
        return {'error': 'Authentication failed'}
    elif isinstance(e, NoBrokersAvailable):
        return {'error': 'No brokers available. Check that your supplied brokers are correct and available.'}
    else:
        return {'error': '{}'.format(e)}


def validateParams(params):
    validatedParams = params.copy()
    requiredParams = ['brokers', 'topic', 'value']
    missingParams = []

    for requiredParam in requiredParams:
        if requiredParam not in params:
            missingParams.append(requiredParam)

    if len(missingParams) > 0:
        return (False, "You must supply all of the following parameters: {}".format(', '.join(missingParams)))

    if isinstance(params['brokers'], str):
        # turn it into a List
        validatedParams['brokers'] = params['brokers'].split(',')

    shuffle(validatedParams['brokers'])

    if 'base64DecodeValue' in params and params['base64DecodeValue'] == True:
        try:
            validatedParams['value'] = base64.b64decode(params['value']).decode('utf-8')
        except:
            return (False, "value parameter is not Base64 encoded")

        if len(validatedParams['value']) == 0:
            return (False, "value parameter is not Base64 encoded")

    if 'base64DecodeKey' in params and params['base64DecodeKey'] == True:
        try:
            validatedParams['key'] = base64.b64decode(params['key']).decode('utf-8')
        except:
            return (False, "key parameter is not Base64 encoded")

        if len(validatedParams['key']) == 0:
            return (False, "key parameter is not Base64 encoded")

    return (True, validatedParams)

def getProducer(validatedParams):
    connectionHash = getConnectionHash(validatedParams)

    if globals().get("cached_producers") is None:
        print("dictionary was none")
        globals()["cached_producers"] = dict()

    # remove arbitrary connection to make room for new one
    if len(globals()["cached_producers"]) == max_cached_producers:
        poppedProducer = globals()["cached_producers"].popitem()[1]
        poppedProducer.close(timeout=1)
        print("Removed cached producer")

    if connectionHash not in globals()["cached_producers"]:
        print("cache miss")
        # create a new connection

        producer = KafkaProducer(
            api_version_auto_timeout_ms=15000,
            batch_size=0,
            bootstrap_servers=validatedParams['brokers'],
            max_block_ms=15000,
            request_timeout_ms=15000,
        )

        print("Created producer")

        # store the producer globally for subsequent invocations
        globals()["cached_producers"][connectionHash] = producer

        # return it
        return producer
    else:
        print("Reusing existing producer")
        return globals()["cached_producers"][connectionHash]


def getConnectionHash(params):
    # always use the sorted brokers to combat the effects of shuffle()
    brokers = params['kafka_brokers_sasl']
    brokers.sort()
    brokersString = ",".join(brokers)

    apiKey = "{}:{}".format(params['user'], params['password'])

    connectionHash = brokersString + apiKey

    return connectionHash
