import requests
import uuid
import json as json_mod

from datetime import datetime

from .database import Database, DBHandle
from .theExceptions import CreationError, ConnectionError
from .users import Users

import grequests


class JWTAuth(requests.auth.AuthBase):
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        # Implement JWT authentication
        r.headers['Authorization'] = 'Bearer %s' % self.token
        return r

class AikidoSession_GRequests(object):
    def __init__(self, username, password, urls):
        if username:
            token = self._get_auth_token(username, password, urls)    
            self.auth = JWTAuth(token)
        else:
            self.auth = None

        self.batching = False
        self.batchedRequests = []

    def _get_auth_token(self, username, password, urls) :
        kwargs = {'data': '{"username":"%s","password":"%s"}' % (username, password)}
        for connection_url in urls:
            response = requests.post('%s/_open/auth' % connection_url, **kwargs)
            if response.ok:
                json_data = response.content
                if json_data:
                    data_dict = json_mod.loads(json_data.decode("utf-8"))
                    auth_token = data_dict.get('jwt')
                    break
        
        return auth_token

    def startBatching(self) :
        self.batching = True
    
    def stopBatching(self) :
        self.batching = False

    def _run(self, req) :
        if self.batching :
            self.batchedRequests.append(req)
            return True
        return grequests.map([req])[0]
    
    def runBatch(self, exception_handler=None) :
        ret = requests.map(self.batchedRequests, exception_handler=exception_handler)
        self.batchedRequests = []
        return ret

    def post(self, url, data=None, json=None, **kwargs):
        if data is not None:
            kwargs['data'] = data
        if json is not None:
            kwargs['json'] = json

        kwargs['auth'] = self.auth

        req = grequests.post(url, **kwargs)
        return self._run(req)
        
    def get(self, url, **kwargs):
        kwargs['auth'] = self.auth
        req = grequests.get(url, **kwargs)
        return self._run(req)

    def put(self, url, data=None, **kwargs):
        if data is not None:
            kwargs['data'] = data
        kwargs['auth'] = self.auth
        req = grequests.put(url, **kwargs)
        return self._run(req)

    def head(self, url, **kwargs):
        kwargs['auth'] = self.auth
        req = grequests.put(url, **kwargs)
        return self._run(req)

    def options(self, url, **kwargs):
        kwargs['auth'] = self.auth
        req = grequests.options(url, **kwargs)
        return self._run(req)

    def patch(self, url, data=None, **kwargs):
        if data is not None:
            kwargs['data'] = data
        kwargs['auth'] = self.auth
        req = grequests.patch(url, **kwargs)
        return self._run(req)

    def delete(self, url, **kwargs):
        kwargs['auth'] = self.auth
        req = grequests.delete(url, **kwargs)
        return self._run(req)

    def disconnect(self):
        pass

class JsonHook(object) :
    """This one replaces requests' original json() function. If a call to json() fails, it will print a message with the request content"""
    def __init__(self, ret) :
        self.ret = ret
        self.ret.json_originalFct = self.ret.json
    
    def __call__(self, *args, **kwargs) :
        try :
            return self.ret.json_originalFct(*args, **kwargs)
        except Exception as e :
            print( "Unable to get json for request: %s. Content: %s" % (self.ret.url, self.ret.content) )
            raise e 

class AikidoSession(object) :
    """Magical Aikido being that you probably do not need to access directly that deflects every http request to requests in the most graceful way.
    It will also save basic stats on requests in it's attribute '.log'.
    """

    class Holder(object) :
        def __init__(self, fct, auth, verify=True) :
            self.fct = fct
            self.auth = auth
            if verify != None:
              self.verify = verify 

        def __call__(self, *args, **kwargs) :
            if self.auth :
                kwargs["auth"] = self.auth
            if self.verify != True:
                kwargs["verify"] = self.verify

            try :
                ret = self.fct(*args, **kwargs)
            except :
                print ("===\nUnable to establish connection, perhaps arango is not running.\n===")
                raise

            if len(ret.content) < 1:
                raise ConnectionError("Empty server response", ret.url, ret.status_code, ret.content)
            elif ret.status_code == 401 :
                raise ConnectionError("Unauthorized access, you must supply a (username, password) with the correct credentials", ret.url, ret.status_code, ret.content)

            ret.json = JsonHook(ret)
            return ret

    def __init__(self, username, password, verify=True) :
        if username :
            self.auth = (username, password)
        else :
            self.auth = None

        self.verify = verify 
        self.session = requests.Session()
        self.log = {}
        self.log["nb_request"] = 0
        self.log["requests"] = {}

    def __getattr__(self, k) :
        try :
            reqFct = getattr(object.__getattribute__(self, "session"), k)
        except :
            raise AttributeError("Attribute '%s' not found (no Aikido move available)" % k)

        holdClass = object.__getattribute__(self, "Holder")
        auth = object.__getattribute__(self, "auth")
        verify = object.__getattribute__(self, "verify")
        log = object.__getattribute__(self, "log")
        log["nb_request"] += 1
        try :
            log["requests"][reqFct.__name__] += 1
        except :
            log["requests"][reqFct.__name__] = 1

        return holdClass(reqFct, auth, verify)

    def disconnect(self) :
        try:
            self.session.close()
        except Exception :
            pass

class Connection(object) :
    """This is the entry point in pyArango and directly handles databases."""
    def __init__(self,
        arangoURL = 'http://127.0.0.1:8529',
        username = None,
        password = None,
        verify = True,
        verbose = False,
        statsdClient = None,
        reportFileName = None,
        loadBalancing = "round-robin",
        use_grequests = True
    ) :
        
        loadBalancingMethods = ["round-robin", "random"]
        if loadBalancing not in loadBalancingMethods :
            raise ValueError("loadBalancing should be one of : %s, got %s" % (loadBalancingMethods, loadBalancing) )
        
        self.loadBalancing = loadBalancing
        self.currentURLId = 0
        self.username = username
        self.use_grequests = use_grequests

        self.databases = {}
        self.verbose = verbose
        
        if type(arangoURL) is str :
            self.arangoURL = [arangoURL]
        else :
            self.arangoURL = arangoURL

        for i, url in enumerate(self.arangoURL) :
            if url[-1] == "/" :
                self.arangoURL[i] = url[:-1]
            
        self.identifier = None
        self.startTime = None
        self.session = None
        self.resetSession(username, password, verify)

        self.users = Users(self)

        if reportFileName != None:
            self.reportFile = open(reportFileName, 'a')
        else:
            self.reportFile = None

        self.statsdc = statsdClient
        self.reload()

    def getEndpointURL(self) :
        if self.loadBalancing == "round-robin" :
            url = self.arangoURL[self.currentURLId]
            self.currentURLId = (self.currentURLId + 1) % len(self.arangoURL)
            return url
        elif self.loadBalancing == "random" :
            import random
            return random.choice(self.arangoURL)

    def getURL(self) :
        return '%s/_api' % self.getEndpointURL()

    def getDatabasesURL(self) :
        if not self.session.auth :
            return '%s/database/user' % self.getURL()
        else :
            return '%s/user/%s/database' % (self.getURL(), self.username)
    
    def updateEndpoints(self, coordinatorURL = None) :
        """udpdates the list of available endpoints from the server"""
        raise NotImplemented("Not done yet.")

    def disconnectSession(self) :
        if self.session: 
            self.session.disconnect()

    def resetSession(self, username=None, password=None, verify=True) :
        """resets the session"""
        self.disconnectSession()
        if self.use_grequests :
            self.session = AikidoSession_GRequests(username, password, self.arangoURL)
        else :
            self.session = AikidoSession(username, password, verify)
        
    def reload(self) :
        """Reloads the database list.
        Because loading a database triggers the loading of all collections and graphs within,
        only handles are loaded when this function is called. The full databases are loaded on demand when accessed
        """

        r = self.session.get(self.getDatabasesURL())

        data = r.json()
        if r.status_code == 200 and not data["error"] :
            self.databases = {}
            for dbName in data["result"] :
                if dbName not in self.databases :
                    self.databases[dbName] = DBHandle(self, dbName)
        else :
            raise ConnectionError(data["errorMessage"], self.getDatabasesURL(), r.status_code, r.content)

    def createDatabase(self, name, **dbArgs) :
        "use dbArgs for arguments other than name. for a full list of arguments please have a look at arangoDB's doc"
        dbArgs['name'] = name
        payload = json_mod.dumps(dbArgs, default=str)
        url = self.getURL() + "/database"
        r = self.session.post(url, data = payload)
        data = r.json()
        if r.status_code == 201 and not data["error"] :
            db = Database(self, name)
            self.databases[name] = db
            return self.databases[name]
        else :
            raise CreationError(data["errorMessage"], r.content)

    def hasDatabase(self, name) :
        """returns true/false wether the connection has a database by the name of 'name'"""
        return name in self.databases

    def __getitem__(self, dbName) :
        """Collection[dbName] returns a database by the name of 'dbName', raises a KeyError if not found"""
        try :
            return self.databases[dbName]
        except KeyError :
            self.reload()
            try :
                return self.databases[dbName]
            except KeyError :
                raise KeyError("Can't find any database named : %s" % dbName)

    def reportStart(self, name):
        if self.statsdc != None:
            self.identifier = str(uuid.uuid5(uuid.NAMESPACE_DNS, name))[-6:]
            if self.reportFile != None:
                self.reportFile.write("[%s]: %s\n" % (self.identifier, name))
                self.reportFile.flush()
            self.startTime = datetime.now()

    def reportItem(self):
        if self.statsdc != None:
            diff = datetime.now() - self.startTime
            microsecs = (diff.total_seconds() * (1000 ** 2) ) + diff.microseconds
            self.statsdc.timing("pyArango_" + self.identifier, int(microsecs))
