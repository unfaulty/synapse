/*
Copyright 2014 matrix.org

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

'use strict';

/*
This service manages where in the event stream the web client currently is,
repolling the event stream, and provides methods to resume/pause/stop the event 
stream. This service is not responsible for parsing event data. For that, see 
the eventHandlerService.
*/
angular.module('eventStreamService', [])
.factory('eventStreamService', ['$q', '$timeout', 'matrixService', 'eventHandlerService', function($q, $timeout, matrixService, eventHandlerService) {
    var END = "END";
    var START = "START";
    var TIMEOUT_MS = 30000;
    var ERR_TIMEOUT_MS = 5000;
    
    var settings = {
        from: "END",
        to: undefined,
        limit: undefined,
        shouldPoll: true,
        isActive: false
    };
    
    // interrupts the stream. Only valid if there is a stream conneciton 
    // open.
    var interrupt = function(shouldPoll) {
        console.log("[EventStream] interrupt("+shouldPoll+") "+
                    JSON.stringify(settings));
        settings.shouldPoll = shouldPoll;
        settings.isActive = false;
    };
    
    var saveStreamSettings = function() {
        localStorage.setItem("streamSettings", JSON.stringify(settings));
    };
    
    var startEventStream = function() {
        settings.shouldPoll = true;
        settings.isActive = true;
        var deferred = $q.defer();
        // run the stream from the latest token
        matrixService.getEventStream(settings.from, TIMEOUT_MS).then(
            function(response) {
                if (!settings.isActive) {
                    console.log("[EventStream] Got response but now inactive. Dropping data.");
                    return;
                }
                
                settings.from = response.data.end;
                
                console.log("[EventStream] Got response from "+settings.from+" to "+response.data.end);
                eventHandlerService.handleEvents(response.data.chunk, true);
                
                deferred.resolve(response);
                
                if (settings.shouldPoll) {
                    $timeout(startEventStream, 0);
                }
                else {
                    console.log("[EventStream] Stopping poll.");
                }
            },
            function(error) {
                if (error.status == 403) {
                    settings.shouldPoll = false;
                }
                
                deferred.reject(error);
                
                if (settings.shouldPoll) {
                    $timeout(startEventStream, ERR_TIMEOUT_MS);
                }
                else {
                    console.log("[EventStream] Stopping polling.");
                }
            }
        );
        return deferred.promise;
    };
    
    return {
        // resume the stream from whereever it last got up to. Typically used
        // when the page is opened.
        resume: function() {
            if (settings.isActive) {
                console.log("[EventStream] Already active, ignoring resume()");
                return;
            }
        
            console.log("[EventStream] resume "+JSON.stringify(settings));
            return startEventStream();
        },
        
        // pause the stream. Resuming it will continue from the current position
        pause: function() {
            console.log("[EventStream] pause "+JSON.stringify(settings));
            // kill any running stream
            interrupt(false);
            // save the latest token
            saveStreamSettings();
        },
        
        // stop the stream and wipe the position in the stream. Typically used
        // when logging out / logged out.
        stop: function() {
            console.log("[EventStream] stop "+JSON.stringify(settings));
            // kill any running stream
            interrupt(false);
            // clear the latest token
            settings.from = END;
            saveStreamSettings();
        }
    };

}]);