import gevent
import gevent.baseserver
import gevent.event
import gevent.timeout
import gevent.pool
import gevent.util
from gservice.util import defaultproperty

import functools

NOT_READY = 1

def require_ready(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            self._ready_event.wait(self.ready_timeout)
        except gevent.timeout.Timeout, e:
            pass
        if not self.ready:
            raise RuntimeWarning("Service must be ready to call this method.")
        return func(self, *args, **kwargs)
    return wrapped

class NamedService(object):
    def __init__(self, name, use_dict):
        self.name = name
        self.use_dict = use_dict

    def __get__(self, instance, owner):
        return self.value

    def __set__(self, instance, value):
        self.value = value

    @property
    def value(self):
        return Service._get_named_service(self.name, self.use_dict)

    @value.setter
    def setvalue(self, value):
        Service.register_named_service(self.name, value, self.use_dict)

    def __str__(self):
        return str(self.value)
        
                

class Service(object):
    """Service base class for creating standalone or composable services
    
    A service is a container for two things: other services and greenlets. It
    then provides a common interface for starting and stopping them, based on
    a subset of the gevent.baseserver interface. This way you can include
    StreamServer or WSGIServer as child services.
    
    Service also lets you catch exceptions in the greenlets started from this
    service and introduces the concept of `ready`, letting us block :meth:`start`
    until the service is actually ready.
    
    """
    stop_timeout = defaultproperty(int, 1)
    ready_timeout = defaultproperty(int, 2)
    started = defaultproperty(bool, False)
    
    _children = defaultproperty(list)
    _stopped_event = defaultproperty(gevent.event.Event)
    _ready_event = defaultproperty(gevent.event.Event)
    _greenlets = defaultproperty(gevent.pool.Group)
    _error_handlers = defaultproperty(dict)

    # main services dictionary for looking up named services
    _main_services = {}

    @classmethod
    def register_named_service(cls, name, service, use_dict=_main_services):
        use_dict[name] = service

    @classmethod
    def _get_named_service(cls, name, use_dict=_main_services):
        return use_dict.get(name, None)

    def __new__(cls, *args, **kwargs):
        """
        Allow for Service('name') to lookup named global services.
        """
        if cls == Service:
            if 'mock_dict' in kwargs:
                use_dict = kwargs['mock_dict']
            else:
                use_dict = Service._main_services

            if 'name' in kwargs:
                name = kwargs['name']
            else:
                name = args[0]
            return NamedService(name, use_dict=use_dict)
        else:
            return super(Service, cls).__new__(cls)
    
    @property
    def ready(self):
        """This property returns whether this service is ready for business"""
        return self._ready_event.isSet()
    
    def set_ready(self):
        """Internal convenience function to proclaim readiness"""
        self._ready_event.set()
    
    def add_service(self, service):
        """Add a child service to this service
        
        The service added will be started when this service starts, before 
        its :meth:`_start` method is called. It will also be stopped when this 
        service stops, before its :meth:`_stop` method is called.
        
        """
        if isinstance(service, gevent.baseserver.BaseServer):
            service = ServiceWrapper(service)
        self._children.append(service)
    
    def remove_service(self, service):
        """Remove a child service from this service"""
        self._children.remove(service)
    
    def _wrap_errors(self, func):
        """Wrap a callable for triggering error handlers
        
        This is used by the greenlet spawn methods so you can handle known
        exception cases instead of gevent's default behavior of just printing
        a stack trace for exceptions running in parallel greenlets.
        
        """
        @functools.wraps(func)
        def wrapped_f(*args, **kwargs):
            exceptions = tuple(self._error_handlers.keys())
            try:
                return func(*args, **kwargs)
            except exceptions, exception:
                for type in self._error_handlers:
                    if isinstance(exception, type):
                        handler, greenlet = self._error_handlers[type]
                        self._wrap_errors(handler)(exception, greenlet)
                return exception
        return wrapped_f
    
    def catch(self, type, handler):
        """Set an error handler for exceptions.
        
        Catches exceptions of `type` raised in greenlets for this service
        and recursively any existing child services.
        """
        self._error_handlers[type] = (handler, gevent.getcurrent())
        for child in self._children:
            child.catch(type, handler)
    
    def spawn(self, func, *args, **kwargs):
        """Spawn a greenlet under this service"""
        func_wrap = self._wrap_errors(func)
        return self._greenlets.spawn(func_wrap, *args, **kwargs)
    
    def spawn_later(self, seconds, func, *args, **kwargs):
        """Spawn a greenlet in the future under this service"""
        group = self._greenlets
        func_wrap = self._wrap_errors(func)
        g = group.greenlet_class(func_wrap, *args, **kwargs)
        g.start_later(seconds)
        group.add(g)
        return g
    
    def start(self, block_until_ready=True):
        """Public interface for starting this service and children. By default it blocks until ready."""
        if self.started:
            raise RuntimeWarning("{} already started".format(
                self.__class__.__name__))
        self._stopped_event.clear()
        self._ready_event.clear()
        try:
            self.pre_start()
            for child in self._children:
                if isinstance(child, Service):
                    if not child.started:
                        child.start(block_until_ready)
                elif isinstance(child, gevent.baseserver.BaseServer):
                    if not child.started:
                        child.start()
            ready = self.do_start()
            if ready == NOT_READY and block_until_ready is True:
                self._ready_event.wait(self.ready_timeout)
            elif ready != NOT_READY:
                self._ready_event.set()
            self.started = True
            self.post_start()
        except:
            self.stop()
            raise
    
    def pre_start(self):
        pass
    
    def post_start(self):
        pass
    
    def do_start(self):
        """Empty implementation of service start. Implement me!
        
        Return `service.NOT_READY` to block until :meth:`set_ready` is
        called (or `ready_timeout` is reached).
        
        """
        return
    
    def stop(self, timeout=None):
        """Stop this service and child services

        If the server uses a pool to spawn the requests, then :meth:`stop` also waits
        for all the handlers to exit. If there are still handlers executing after *timeout*
        has expired (default 1 second), then the currently running handlers in the pool are killed."""
        if gevent.getcurrent() in self._greenlets:
            return gevent.spawn(self.stop)
        self.started = False
        try:
            self.pre_stop()
            for child in reversed(self._children):
                #iterate over children in reverse order, in case dependancies
                # were implied by the starting order
                if child.started:
                    child.stop()
            self.do_stop()
        finally:
            if timeout is None:
                timeout = self.stop_timeout
            if self._greenlets:
                self._greenlets.join(timeout=timeout)
                self._greenlets.kill(block=True, timeout=1)
            self._ready_event.clear()
            self._stopped_event.set()
            self.post_stop()
    
    def pre_stop(self):
        pass
    
    def post_stop(self):
        pass
    
    def do_stop(self):
        """Empty implementation of service stop. Implement me!"""
        return
    
    def reload(self):
        for child in self._children:
            child.reload()
        self.do_reload()
    
    def do_reload(self):
        """Empty implementation of service reload. Implement me!"""
        pass

    def serve_forever(self, stop_timeout=None, ready_callback=None):
        """Start the service if it hasn't been already started and wait until it's stopped."""
        if not self.started:
            self.start()
            if ready_callback:
                ready_callback()
        try:
            self._stopped_event.wait()
        except:
            self.stop(timeout=stop_timeout)
            raise
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, type, value, traceback):
        self.stop()

class ServiceWrapper(Service):
    """Wrapper for gevent servers that are based on gevent.baseserver.BaseServer
    
    Although the Service class mostly looks like the BaseServer interface,
    there are certain extra methods (like exception catching) that are assumed
    to be available. This class allows us to wrap gevent servers so they actually
    are a Service. Ideally, you would never use this directly. For example, it's
    being used in Service.add_service to automatically wrap gevent servers passed in.
    """
    def __init__(self, klass_or_server, *args, **kwargs):
        super(ServiceWrapper, self).__init__()
        if isinstance(klass_or_server, gevent.baseserver.BaseServer):
            self.wrapped = klass_or_server
        else:
            self.wrapped = klass(*args, **kwargs)
    
    def do_start(self):
        self.spawn(self.wrapped.start)
    
    def do_stop(self):
        self.wrapped.stop()
