var request = require('request');

/**
 *   Feed to listen to Kafka messages
 *  @param {string} brokers - array of Kafka brokers
 *  @param {string} topic - topic to subscribe to
 *  @param {bool}   isJSONData - attempt to parse messages as JSON
 *  @param {string} endpoint - address to OpenWhisk deployment
 */
function main(params) {
    if(!params.package_endpoint) {
        whisk.error('Could not find the package_endpoint parameter.');
        return;
    }

    var triggerComponents = params.triggerName.split("/");
    var namespace = encodeURIComponent(triggerComponents[1]);
    var trigger = encodeURIComponent(triggerComponents[2]);

    if (namespace === "_") {
        whisk.error('You must supply a non-default namespace.');
        return;
    }

    var feedServiceURL = 'http://' + params.package_endpoint + '/triggers/' + namespace + '/' + trigger;

    if (params.lifecycleEvent === 'CREATE') {
        var validatedParams = validateParameters(params);
        if (!validatedParams) {
            // whisk.error has already been called.
            // all that remains is to bail out.
            return;
        }

        var body = validatedParams;
        // params.endpoint may already include the protocol - if so,
        // strip it out
        var massagedAPIHost = massageAPIHost(params.endpoint);
        body.triggerURL = 'https://' + whisk.getAuthKey() + "@" + massagedAPIHost + '/api/v1/namespaces/' + namespace + '/triggers/' + trigger;

        var options = {
            method: 'PUT',
            url: feedServiceURL,
            body: JSON.stringify(body),
            headers: {
                'Content-Type': 'application/json',
                'User-Agent': 'whisk'
            }
        };

        return doRequest(options);
    } else if (params.lifecycleEvent === 'DELETE') {
        var authorizationHeader = 'Basic ' + new Buffer(whisk.getAuthKey()).toString('base64');

        var options = {
            method: 'DELETE',
            url: feedServiceURL,
            headers: {
                'Content-Type': 'application/json',
                'Authorization': authorizationHeader,
                'User-Agent': 'whisk'
            }
        };

        return doRequest(options)
    }
}

function doRequest(options) {
    var promise = new Promise(function (resolve, reject) {
        request(options, function (error, response, body) {
            if (error) {
                reject({
                    response: response,
                    error: error,
                    body: JSON.parse(body)
                });
            } else {
                console.log("Status code: " + response.statusCode);

                if (response.statusCode >= 400) {
                    console.log("Response from Kafka feed service: " + body);
                    reject({
                        statusCode: response.statusCode,
                        response: JSON.parse(body)
                    });
                } else {
                    resolve({
                        response: JSON.parse(body)
                    });
                }
            }
        });
    });

    return promise;
}

function validateParameters(rawParams) {
    var validatedParams = {};

    validatedParams.isJSONData = (typeof rawParams.isJSONData !== 'undefined' && rawParams.isJSONData && (rawParams.isJSONData === true || rawParams.isJSONData.toString().trim().toLowerCase() === 'true'))

    if (rawParams.topic && rawParams.topic.length > 0) {
        validatedParams.topic = rawParams.topic;
    } else {
        whisk.error('You must supply a "topic" parameter.');
        return;
    }

    validatedParams.isMessageHub = false;

    if (isNonEmptyArray(rawParams.brokers)) {
        validatedParams.brokers = rawParams.brokers;
    } else {
        whisk.error('You must supply a "brokers" parameter as an array of Kafka brokers.');
        return;
    }

    return validatedParams;
}

function isNonEmptyArray(obj) {
    return obj && Array.isArray(obj) && obj.length !== 0;
}

// if the apiHost already includes the protocol, remove it
function massageAPIHost(apiHost) {
    if(apiHost.substr(0, 4) === 'http') {
        // the apiHost includes the protocol - strip it out
        return apiHost.split('/')[2]
    } else {
        return apiHost;
    }
}
