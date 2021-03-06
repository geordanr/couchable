# Copyright (c) 2010 Eli Stevens
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.


"""
foo
bar
"""

import collections
import copy
import cPickle as pickle
import cStringIO
import datetime
import gzip
import hashlib
import inspect
import itertools
import os
import pprint
import re
import string
import subprocess
import sys
import tempfile
import time
import uuid
import weakref

#import yaml
import couchdb
import couchdb.client
import couchdb.design
#import couchdb.mapping

"""
import couchable
import couchdb
server = couchdb.Server()
try:
    cdb = server['pykour']
except:
    cdb = server.create('pykour')

class C(couchable.Couchable):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

c=C(foo=1,bar=2,_baz=3)
d=C(_c=c)
e=C(c=c, d=d, couchable_foo={'couchable_bar':9})
couchable.store(e, cdb)
print 'c', c._id
print 'd', d._id
print 'e', e._id
i=e._id
del c
del d
del e
e=couchable.load(i, cdb)
"""

def importstr(module_str, from_=None):
    """
    >>> importstr('os')
    <module 'os' from '.../os.pyc'>
    >>> importstr('math', 'fabs')
    <built-in function fabs>
    """
    module = __import__(module_str)
    for sub_str in module_str.split('.')[1:]:
        module = getattr(module, sub_str)

    if from_:
        return getattr(module, from_)
    return module

def typestr(type_):
    if not isinstance(type_, type):
        type_ = type(type_)
    if type_.__name__ in __builtins__:
        return type_.__name__
    else:
        return '{}.{}'.format(type_.__module__, type_.__name__)

FIELD_NAME = 'couchable:'

class UncouchableException(Exception):
    def __init__(self, msg, cls, obj):
        Exception.__init__(self, msg)
        self.cls = cls
        self.obj = obj

# type packing / unpacking
_pack_handlers = collections.OrderedDict()
def _packer(*args):
    def func(func_):
        for type_ in args:
            packer(type_, func_)
        return func_
    return func

def packer(type_, func_):
    """
    This function is still in potential flux.  Writing new (un)packers is delicate; please see the source.
    """
    _pack_handlers[type_] = func_
    _pack_handlers[typestr(type_)] = func_

#_unpack_handlers = collections.OrderedDict()
#def _unpacker(*args):
#    def func(func_):
#        for type_ in args:
#            unpacker(type_, func_)
#        return func_
#    return func
#
#def unpacker(type_, func_):
#    """
#    This function is still in potential flux.  Writing new (un)packers is delicate; please see the source.
#    """
#    _unpack_handlers[type_] = func_
#    _unpack_handlers[typestr(type_)] = func_



# function for navigating the above dics of handlers, etc.
def findHandler(cls_or_name, handler_dict):
    """
    >>> class A(object): pass
    ...
    >>> class B(A): pass
    ...
    >>> class C(object): pass
    ...
    >>> handlers={A:'AAA'}
    >>> findHandler(A, handlers)
    (<class 'couchable.core.A'>, 'AAA')
    >>> findHandler(B, handlers)
    (<class 'couchable.core.A'>, 'AAA')
    >>> findHandler(C, handlers)
    (None, None)
    """
    #if isinstance(cls_or_name, basestring):
    #    for type_, handler in reversed(handler_dict.items()):
    #        if cls_or_name == str(type_):
    #            return type_, handler
    #el
    if cls_or_name in handler_dict:
        return cls_or_name, handler_dict[cls_or_name]
    else:
        for type_, handler in reversed(handler_dict.items()):
            if isinstance(type_, type) and isinstance(cls_or_name, type) and issubclass(cls_or_name, type_):
                return type_, handler

    return None, None

class CouchableDb(object):
    """
    Currently, though it is not documented here, the .db parameter is part of
    the public API of CouchableDb; it is required for use with views, etc.
    Please see the couchdb documentation for details:

    U{http://packages.python.org/CouchDB/}

    It is possible that in the future couchdbkit could also be used:

    U{http://couchdbkit.org/}
    """

    _obj_by_id_cache = weakref.WeakValueDictionary()

    def __init__(self, name=None, url='http://localhost:5984/', db=None):
        """
        Creates a CouchableDb wrapper around a couchdb.Database object.  If
        the database does not yet exist, it will be created.

        @type  name: str
        @param name: Name of the CouchDB database to connect to.
        @type  url: str
        @param url: The URL of the CouchDB server.  Uses the couchdb default of http://localhost:5984/
        @type  db: couchdb.Database
        @param db: An instance of couchdb.Database that has already been instantiated.  Overrides the name and url params.
        """

        # This dance is odd due to the semantics of how WVD works.
        cache_key = (url, name)
        self._obj_by_id = self._obj_by_id_cache.get(cache_key, weakref.WeakValueDictionary())
        self._obj_by_id_cache[(url, name)] = self._obj_by_id

        self.name = name
        self.url = url

        if db is None:
            self.server = couchdb.Server(self.url)

            try:
                db = self.server[self.name]
            except:
                db = self.server.create(self.name)

        self.db = db

        self._init_views()

    def _init_views(self):
        byclass_js = '''
            function(doc) {
                if ('couchable:' in doc) {
                    var info = doc['couchable:'];
                    emit([info.module, info.class, doc._id], doc);
                }
            }'''

        couchdb.design.ViewDefinition('couchable', 'byclass', byclass_js).sync(self.db)

    def addClassView(self, cls, name, keys=None, multikeys=None, value='1', reduce=None):
        """
        Creates a view that only emits records for documents of the specified
        class.  Each record also emits keys based on the parameters given, which
        can be used for things like "get all Foo instances with bar between 3
        and 7."

        The view code resembles the following::

            function(doc) {
                if ('couchable:' in doc) {
                    var info = doc['couchable:'];
                    if (info.module == '$module' && info.class == '$cls') {
                        $emit
                    }
                }
            }

        I{This behavior may change during the course of the 0.x.x series of releases.}

        @type  cls: type
        @param cls: The class of objects that the view should be restricted to.  Note that sub/superclasses are not considered.
        @type  name: string
        @param name: The string to suffix the name of the view with (byclass:module.class:name).
        @type  keys: list of strings
        @param keys: A list of unescaped javascript expressions to use as the key for the view.
        @type  multikeys: list of list of strings
        @param multikeys: A list of keys (see above).  Each key will get a separate emit.
        @type  value: string
        @param value: A string of unescaped javascript used as the value of each emit.
        @type  reduce: string
        @param reduce: A CouchDB reduce function.  Can be None, javascript, or the built-in '_sum' kind of reduce function.
        @rtype: str
        @return: The full name of the view (byclass:module.class:name).
        """
        multikeys = multikeys or [keys]
        emit_js = '\n'.join(['''emit([{}], {});'''.format(', '.join([('info.private.' + key if key[0] == '_' else 'doc.' + key) for key in keys]), value) for keys in multikeys])

        byclass_js = '''
            function(doc) {
                if ('couchable:' in doc) {
                    var info = doc['couchable:'];
                    if (info.module == '$module' && info.class == '$cls') {
                        $emit
                    }
                }
            }'''

        byclass_js = string.Template(byclass_js).safe_substitute(module=cls.__module__, cls=cls.__name__, emit=emit_js, value=value)

        fullName = 'byclass:{}.{}:{}'.format(cls.__module__, cls.__name__, name)
        couchdb.design.ViewDefinition('couchable', fullName, byclass_js, reduce).sync(self.db)

        return fullName

    #@deprecated
    def loadInstances(self, cls):
        return self.load(self.db.view('couchable/byclass', include_docs=True, startkey=[cls.__module__, cls.__name__], endkey=[cls.__module__, cls.__name__, {}]).rows)

    def __deepcopy__(self, memo):
        return copy.copy(self)

    #    cls = type(self)
    #    inst = cls.__new__(cls)
    #    inst.__dict__.update({copy.deepcopy(k): copy.deepcopy(v) for k,v in self.__dict__.items if k not in ['_cdb']})


    def store(self, what):
        """
        Stores the documents in the C{what} parameter in CouchDB.  If a C{._id}
        does not yet exist on the object, it will be added.  If the C{._id} is
        present, it will be used instead.  The C{._rev} of the object(s) must
        match what is already in the database.

        Any attachments for the document will also be uploaded.  As of the
        current revision (0.0.1b2), each attachment will be uploaded each time
        the document is stored.

        I{This behavior is expected to change during the course of the 0.x.x series of releases.}

        Any objects referenced by the object(s) in C{what} will also be stored.
        If those objects are L{registered as document types<registerDocType>},
        then they will also be stored as top level objects, even if they exist
        in the database already, and have not changed.

        I{This behavior may change during the course of the 0.x.x series of releases.}

        Any cycles comprised entirely of non-document classes will cause the
        store call to raise an exception.  Cycles where at least one object in
        the cycle is to be stored as a top-level document are fine.

        I{This behavior may change during the course of the 0.x.x series of releases.}

        @type  what: obj or list
        @param what: The object or list of objects to store in CouchDB.
        @rtype: str or list
        @return: The C{._id} of the C{what} parameter, or the list of such IDs if C{what} was a list.
        """
        if not isinstance(what, list):
            store_list = [what]
        else:
            store_list = what

        self._done_dict = collections.OrderedDict()

        for obj in store_list:
            self._store(obj)

        # Actually (finally) send the data to couchdb.
        #try:
            #pprint.pprint([(x[0]._id, getattr(x[0], '_rev', None)) for x in self._done_dict.values()])
            #print datetime.datetime.now(), "214: self.db.update"
        ret_list = self.db.update([x[1] for x in self._done_dict.values()])
        #except:
        #    print self._done_dict.values()
        #    raise

        for ret, store_tuple in itertools.izip(ret_list, self._done_dict.values()):
            success, _id, _rev = ret
            obj, doc, attachment_list = store_tuple
            if success:
                for content, content_name, content_type in attachment_list:
                    #print datetime.datetime.now(), "225: self.db.put_attachment"
                    self.db.put_attachment(doc, content, content_name, content_type)

                # This is important, even if there are no attachments
                obj._rev = doc['_rev']
            else:
                raise _rev # it's actually an exception
                #print "Error:", ret
                #print "\tobj:", getattr(obj, '_rev', None), "vs. db:", self.db[_id]['_rev']

            self._obj_by_id[obj._id] = obj

        del self._done_dict

        if not isinstance(what, list):
            return what._id
        else:
            return [obj._id for obj in store_list]


    def _store(self, obj):
        if isinstance(obj, (CouchableDb, couchdb.client.Server, couchdb.client.Database)):
            raise UncouchableException("Illegal to attempt to store objects of type", type(obj), obj)

        base_cls, func_tuple = findHandler(type(obj), _couchable_types)
        if func_tuple:
            func_tuple[0](obj, self)

        if not hasattr(obj, '_id'):
            obj._id = '{}:{}'.format(typestr(obj), uuid.uuid4()).lstrip('_')
            assert obj._id not in self._obj_by_id

        if obj._id not in self._done_dict:
            self._done_dict[obj._id] = (obj, {}, [])

            attachment_list = []
            # This code matches the code in _pack_object
            #doc = self._obj2doc_empty(obj)
            #doc.update(self._pack_dict_keyMeansObject(doc, obj.__dict__, attachment_list, '', True))

            doc = {}
            doc.update(self._pack_dict_keyMeansObject(doc, obj.__dict__, attachment_list, '', True))
            doc = self._objInfo_doc(obj, doc)

            #self._pack(doc, obj, attachment_list)

            self._done_dict[obj._id] = (obj, doc, attachment_list)

            obj._cdb = self


    def _pack(self, parent_doc, data, attachment_list, name, isKey=False):
        cls = type(data)

        base_cls, handler = findHandler(cls, _pack_handlers)

        return handler(self, parent_doc, data, attachment_list, name, isKey)
        #if handler:
        #    try:
        #        return handler(self, parent_doc, data, attachment_list, name, isKey)
        #    except RuntimeError:
        #        print "Error with", cls, data
        #        raise
        #else:
        #    raise UncouchableException("No _packer for type", cls, data)

        #if cls in _pack_handlers:
        #    return _pack_handlers[cls](self, parent_doc, data, attachment_list, name, isKey)
        #else:
        #    for types, func in reversed(_pack_handlers.items()):
        #        if isinstance(data, types):
        #            return func(self, parent_doc, data, attachment_list, name, isKey)
        #            break
        #    else:
        #        raise UncouchableException("No _packer for type", cls, data)

    def _objInfo_doc(self, data, doc):
        """
        >>> cdb=CouchableDb('testing')
        >>> obj = object()
        >>> pprint.pprint(cdb._objInfo_doc(obj, {}))
        {'couchable:': {'class': 'object', 'module': '__builtin__'}}
        """
        cls = type(data)
        doc.setdefault(FIELD_NAME, {})
        doc[FIELD_NAME]['class'] = cls.__name__

        if hasattr(cls, '__module__'):
            doc[FIELD_NAME]['module'] = str(cls.__module__)

        try:
            doc[FIELD_NAME]['src_md5'] = hashlib.md5(inspect.getsource(cls)).hexdigest()
        except (IOError, TypeError):
            pass

        return doc

    def _objInfo_consargs(self, data, doc, args=None, kwargs=None):
        """
        >>> cdb=CouchableDb('testing')
        >>> obj = tuple([1, 2, 3])
        >>> pprint.pprint(cdb._objInfo_consargs(obj, {}, list(obj), {}))
        {'couchable:': {'args': [1, 2, 3],
                        'class': 'tuple',
                        'kwargs': {},
                        'module': '__builtin__'}}
        """
        doc = self._objInfo_doc(data, doc)
        doc[FIELD_NAME]['args'] = args or []
        doc[FIELD_NAME]['kwargs'] = kwargs or {}

        return doc

    #def _obj2doc_dict(self, data):
    #    doc = self._obj2doc_empty(data)
    #
    #    return doc


    # This needs to be first, so that it's the last to match in _pack(...)
    @_packer(object)
    def _pack_object(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []
        >>> class Foo(object):
        ...     def __init__(self):
        ...         self.a = 'a'
        ...         self.b = u'b'
        ...         self.c = 'couchable:'
        ...         self.d = {1:2, (3,4,5):(6,7)}
        ...
        >>> data = Foo()
        >>> pprint.pprint(cdb._pack_object(parent_doc, data, attachment_list, 'myname', False))
        {'a': 'a',
         'b': u'b',
         'c': 'couchable:append:str:couchable:',
         'couchable:': {'class': 'Foo', 'module': 'couchable.core'},
         'd': {'couchable:key:tuple:(3, 4, 5)': {'couchable:': {'args': [[6, 7]],
                    'class': 'tuple',
                    'kwargs': {},
                    'module': '__builtin__'}},
               'couchable:repr:int:1': 2}}
        >>> pprint.pprint(parent_doc)
        {'couchable:': {'keys': {'couchable:key:tuple:(3, 4, 5)': {'couchable:':
            {'args': [[3, 4, 5]],
                'class': 'tuple',
                'kwargs': {},
                'module': '__builtin__'}}}}}
        """
        cls = type(data)
        base_cls, callback_tuple = findHandler(cls, _couchable_types)

        if base_cls:
            self._store(data)

            return '{}{}:{}'.format(FIELD_NAME, 'id', data._id)
        else:
            if isKey:
                key_str = '{}{}:{}:{!r}'.format(FIELD_NAME, 'key', typestr(cls), data)

                parent_doc.setdefault(FIELD_NAME, {})
                parent_doc[FIELD_NAME].setdefault('keys', {})
                parent_doc[FIELD_NAME]['keys'][key_str] = self._pack_object(parent_doc, data, attachment_list, name, False)

                return key_str
            elif not hasattr(data, '__dict__'):
                return self._pack_attachment(parent_doc, data, attachment_list, name, isKey)
            else:
                # This code matches the code in _store
                doc = {}
                doc.update(self._pack_dict_keyMeansObject(parent_doc, data.__dict__, attachment_list, name, True))
                doc = self._objInfo_doc(data, doc)

                return doc

    @_packer(type(os))
    def _pack_module(self, parent_doc, data, attachment_list, name, isKey):
        """
        >> import os.path
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []

        >>> data = os.path
        >>> cdb._pack_module(parent_doc, data, attachment_list, 'myname', False)
        'couchable:module:os.path'
        >>> cdb._pack_module(parent_doc, data, attachment_list, 'myname', True)
        'couchable:module:os.path'
        """

        for name, module in sys.modules.items():
            if module == data:
                return '{}{}:{}'.format(FIELD_NAME, 'module', name)


    @_packer(str, unicode)
    def _pack_native(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []

        >>> data = 'byte string'
        >>> cdb._pack_native(parent_doc, data, attachment_list, 'myname', False)
        'byte string'
        >>> cdb._pack_native(parent_doc, data, attachment_list, 'myname', True)
        'byte string'

        >>> data = u'unicode string'
        >>> cdb._pack_native(parent_doc, data, attachment_list, 'myname', False)
        u'unicode string'
        >>> cdb._pack_native(parent_doc, data, attachment_list, 'myname', True)
        u'unicode string'

        >>> data = 'couchable:must escape this'
        >>> cdb._pack_native(parent_doc, data, attachment_list, 'myname', False)
        'couchable:append:str:couchable:must escape this'
        """

        if data.startswith(FIELD_NAME):
            return '{}{}:{}:{}'.format(FIELD_NAME, 'append', typestr(data), data)
        else:
            return data

    @_packer(int, long, float, type(None))
    def _pack_native_keyAsRepr(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []
        >>> data = 1234
        >>> cdb._pack_native_keyAsRepr(parent_doc, data, attachment_list, 'myname', False)
        1234
        >>> cdb._pack_native_keyAsRepr(parent_doc, data, attachment_list, 'myname', True)
        'couchable:repr:int:1234'
        >>> data = 12.34
        >>> cdb._pack_native_keyAsRepr(parent_doc, data, attachment_list, 'myname', False)
        12.34
        >>> cdb._pack_native_keyAsRepr(parent_doc, data, attachment_list, 'myname', True)
        'couchable:repr:float:12.34'
        """
        if isKey:
            return '{}{}:{}:{!r}'.format(FIELD_NAME, 'repr', typestr(data), data)
        else:
            return data

    @_packer(tuple, frozenset, set)
    def _pack_consargs_keyAsKey(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []

        >>> data = tuple([1, 2, 3])
        >>> pprint.pprint(cdb._pack_consargs_keyAsKey(parent_doc, data, attachment_list, 'myname', False))
        {'couchable:':
            {'args': [[1, 2, 3]],
                'class': 'tuple',
                'kwargs': {},
                'module': '__builtin__'}}
        >>> pprint.pprint(parent_doc)
        {}
        >>> pprint.pprint(cdb._pack_consargs_keyAsKey(parent_doc, data, attachment_list, 'myname', True))
        'couchable:key:tuple:(1, 2, 3)'
        >>> pprint.pprint(parent_doc)
        {'couchable:': {'keys': {'couchable:key:tuple:(1, 2, 3)': {'couchable:':
            {'args': [[1, 2, 3]],
                'class': 'tuple',
                'kwargs': {},
                'module': '__builtin__'}}}}}

        >>> parent_doc = {}
        >>> data = frozenset([1, 2, 3])
        >>> pprint.pprint(cdb._pack_consargs_keyAsKey(parent_doc, data, attachment_list, 'myname', False))
        {'couchable:':
            {'args': [[1, 2, 3]],
                'class': 'frozenset',
                'kwargs': {},
                'module': '__builtin__'}}
        >>> pprint.pprint(parent_doc)
        {}
        >>> cdb._pack_consargs_keyAsKey(parent_doc, data, attachment_list, 'myname', True)
        'couchable:key:frozenset:frozenset([1, 2, 3])'
        >>> pprint.pprint(parent_doc)
        {'couchable:': {'keys': {'couchable:key:frozenset:frozenset([1, 2, 3])': {'couchable:':
            {'args': [[1, 2, 3]],
                'class': 'frozenset',
                'kwargs': {},
                'module': '__builtin__'}}}}}
        """
        if isKey:
            key_str = '{}{}:{}:{!r}'.format(FIELD_NAME, 'key', typestr(data), data)

            parent_doc.setdefault(FIELD_NAME, {})
            parent_doc[FIELD_NAME].setdefault('keys', {})
            parent_doc[FIELD_NAME]['keys'][key_str] = self._pack_consargs_keyAsKey(parent_doc, data, attachment_list, name, False)

            return key_str
        else:
            return self._objInfo_consargs(data, {}, [self._pack_list_noKey(parent_doc, list(data), attachment_list, name, False)])

    @_packer(list)
    def _pack_list_noKey(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []

        >>> data = [1, 2, 3]
        >>> cdb._pack_list_noKey(parent_doc, data, attachment_list, 'myname', False)
        [1, 2, 3]

        >>> data = [1, 2, (3, 4, 5)]
        >>> pprint.pprint(cdb._pack_list_noKey(parent_doc, data, attachment_list, 'myname', False))
        [1,
         2,
         {'couchable:': {'args': [[3, 4, 5]],
                         'class': 'tuple',
                         'kwargs': {},
                         'module': '__builtin__'}}]
        >>> pprint.pprint(parent_doc)
        {}
        """
        assert not isKey
        return [self._pack(parent_doc, x, attachment_list, '{}[{}]'.format(name, i), False) for i, x in enumerate(data)]

    @_packer(dict)
    def _pack_dict_keyMeansObject(self, parent_doc, data, attachment_list, name, isObjDict):
        """
        >>> cdb=CouchableDb('testing')
        >>> parent_doc = {}
        >>> attachment_list = []

        >>> data = {'a': 'b', 'couchable:':'c'}
        >>> pprint.pprint(cdb._pack_dict_keyMeansObject(parent_doc, data, attachment_list, 'myname', False))
        {'a': 'b', 'couchable:append:str:couchable:': 'c'}

        >>> data = {1:1, 2:2, 3:(3, 4, 5)}
        >>> pprint.pprint(cdb._pack_dict_keyMeansObject(parent_doc, data, attachment_list, 'myname', False))
        {'couchable:repr:int:1': 1,
         'couchable:repr:int:2': 2,
         'couchable:repr:int:3': {'couchable:': {'args': [[3, 4, 5]],
                                            'class': 'tuple',
                                            'kwargs': {},
                                            'module': '__builtin__'}}}
        >>> data = {(3, 4, 5):3}
        >>> pprint.pprint(cdb._pack_dict_keyMeansObject(parent_doc, data, attachment_list, 'myname', False))
        {'couchable:key:tuple:(3, 4, 5)': 3}
        >>> pprint.pprint(parent_doc)
        {'couchable:': {'keys': {'couchable:key:tuple:(3, 4, 5)': {'couchable:':
            {'args': [[3, 4, 5]],
                'class': 'tuple',
                'kwargs': {},
                'module': '__builtin__'}}}}}
        """
        if isObjDict:
            private_keys = {k for k in data.keys() if k.startswith('_') and k not in ('_id', '_rev', '_attachments', '_cdb')}
        else:
            private_keys = set()

        doc = {self._pack(parent_doc, k, attachment_list, '{}>{}'.format(name, str(k)), True):
            self._pack(parent_doc, v, attachment_list, '{}.{}'.format(name, str(k)), False)
            for k,v in data.items() if k not in private_keys and k not in ['_attachments', '_cdb']}

        #assert '_attachments' not in doc, ', '.join([str(data), str(isObjDict)])

        if private_keys:
            doc.setdefault(FIELD_NAME, {})
            #doc[FIELD_NAME].setdefault('private', {})
            doc[FIELD_NAME]['private'] = {self._pack(parent_doc, k, attachment_list, '{}>{}'.format(name, str(k)), True):
                self._pack(parent_doc, v, attachment_list, '{}.{}'.format(name, str(k)), False)
                for k,v in data.items() if k in private_keys}
            #parent_doc.setdefault(FIELD_NAME, {})
            #parent_doc[FIELD_NAME]['private'] = {self._pack(parent_doc, k, attachment_list, '{}>{}'.format(name, str(k)), True):
            #    self._pack(parent_doc, v, attachment_list, '{}.{}'.format(name, str(k)), False)
            #    for k,v in data.items() if k in private_keys}

        return doc

    def _pack_attachment(self, parent_doc, data, attachment_list, name, isKey):
        cls = type(data)

        base_cls, handler_tuple = findHandler(cls, _attachment_handlers)

        if base_cls is None:
            content = pickle.dumps(data)
            attachment_list.append((content, name, 'application/octet-stream'))
            return '{}{}:{}:{}'.format(FIELD_NAME, 'attachment', 'pickle', name)
        else:
            content = handler_tuple[0](data)
            attachment_list.append((content, name, handler_tuple[2]))
            return '{}{}:{}:{}'.format(FIELD_NAME, 'attachment', typestr(base_cls), name)

    def _unpack(self, parent_doc, doc, loaded_dict, inst=None):
        try:
            if isinstance(doc, (str, unicode)):
                if doc.startswith(FIELD_NAME):
                    _, method_str, data = doc.split(':', 2)

                    if method_str == 'id':
                        return self._load(data, loaded_dict)

                    elif method_str == 'module':
                        return importstr(data)

                    type_str, data = data.split(':', 1)
                    if method_str == 'append':
                        if type_str == 'unicode':
                            return unicode(data, 'utf8')
                        if type_str == 'str':
                            return data

                    elif method_str == 'repr':
                        if type_str in __builtins__:
                            return __builtins__.get(type_str)(data)
                        elif type_str == '__builtin__.NoneType':
                            return None
                        #else:
                        #    return importstr(*type_str.rsplit('.', 1))(data)

                    elif method_str == 'key':
                        return self._unpack(parent_doc, parent_doc[FIELD_NAME]['keys'][doc], loaded_dict)

                    elif method_str == 'attachment':
                        if type_str == 'pickle':
                            attachment_response = self.db.get_attachment(parent_doc, data)
                            return pickle.loads(attachment_response.read())
                        if type_str == '__builtin__.NoneType':
                            attachment_response = self.db.get_attachment(parent_doc, data)
                            return pickle.loads(attachment_response.read())
                        else:
                            base_cls, handler_tuple = findHandler(type_str, _attachment_handlers)
                            attachment_response = self.db.get_attachment(parent_doc, data)
                            return handler_tuple[1](attachment_response.read())
                    else:
                        # FIXME: error?
                        pass

                else:
                    return doc

            elif isinstance(doc, (int, float)):
                return doc

            elif isinstance(doc, list):
                return [self._unpack(parent_doc, x, loaded_dict) for x in doc]

            elif isinstance(doc, dict):
                if FIELD_NAME in doc:
                    info = doc[FIELD_NAME]
                    #del doc[FIELD_NAME]

                    cls = importstr(info['module'], info['class'])

                    if 'args' in info and 'kwargs' in info:
                        #print cls, doc['args'], doc['kwargs']
                        inst = cls(*info['args'], **info['kwargs'])
                    else:
                        if inst is None:
                            inst = cls.__new__(cls)
                            # This is important, see test_docCycles
                            #print doc
                            if '_id' in doc:
                                self._obj_by_id[doc['_id']] = inst

                        #print "unpack isinstance(doc, dict) doc:", doc.get('_id', 'still no id')
                        #print "unpack isinstance(doc, dict) doc:", doc.get('_rev', 'still no rev')

                        inst.__dict__.update(info.get('private', {}))
                        if '_id' in doc:
                            inst.__dict__['_id'] = doc['_id']
                            inst.__dict__['_rev'] = doc['_rev']

                        # If we haven't stuffed the cache AND pre-set the id/rev, then this goes into an infinite loop.  See test_docCycles
                        inst.__dict__.update({self._unpack(parent_doc, k, loaded_dict): self._unpack(parent_doc, v, loaded_dict) for k,v in doc.items() if k != FIELD_NAME})

                        #print "unpack isinstance(doc, dict) inst:", inst.__dict__.get('_id', 'still no id')
                        #print "unpack isinstance(doc, dict) inst:", inst.__dict__.get('_rev', 'still no rev')

                    #print "Unpacking:", inst
                    return inst

                else:
                    return {self._unpack(parent_doc, k, loaded_dict): self._unpack(parent_doc, v, loaded_dict) for k,v in doc.items()}
        except:
            print "Error with:", doc
            raise

    def load(self, what, loaded=None):
        """
        Loads the indicated object(s) out of CouchDB.

        Loading an ID multiple times will result in getting the same object
        returned each time.  Subsequent loads will return the same object
        again, but with an updated C{__dict__}.  Note that this means it is
        impossible to have both the current version of the object and an older
        revision loaded at the same time.

        I{Behavior of loading old document revisions is untested at this time.}

        If what is a dict or a couchdb.client.Row, then the values will be used
        from that object rather than re-fetching from the database.  Likewise,
        the loaded parameter can be used to prevent multiple DB hits.  This can
        be useful when loading multiple documents returned by a view, etc.

        Example use::

            cdb.load(cdb.db.view('couchable/' + viewName, include_docs=True, startkey=[...], endkey=[..., {}]).rows)

        @type  what: str, dict, couchdb.client.Row or list of same
        @param what: A document C{_id}, a dict with an C{'_id'} key, a couchdb.client.Row instance, or a list of any of the preceding.
        @type  loaded: dict, couchdb.client.Row or list of same
        @param loaded: A mapping of document C{_id}s to documents that have already been loaded out of the database.
        @rtype: obj or list
        @return: The object indicated by the C{what} parameter, or a list of such objects if C{what} was a list.
        """
        id_list = []

        if isinstance(loaded, list):
            loaded_dict = {(x.id if isinstance(x, couchdb.client.Row) else x['_id']): (x.doc if isinstance(x, couchdb.client.Row) else x) for x in loaded}
        else:
            loaded_dict = loaded or {}

        #if not isinstance(what, (list, couchdb.client.ViewResults)):
        if not isinstance(what, list):
            load_list = [what]
        else:
            load_list = what

        for item in load_list:
            #print "item", item
            if isinstance(item, basestring):
                id_list.append(item)
            elif isinstance(item, couchdb.client.Row):
                id_list.append(item.id)

                if hasattr(item, 'doc'):
                    loaded_dict[item.id] = item.doc

            elif isinstance(item, dict):
                id_list.append(item['_id'])

                if len(item) > 2:
                    loaded_dict[item['_id']] = item

        if not isinstance(what, list):
            #print "what", what
            return [self._load(_id, loaded_dict) for _id in id_list][0]
        else:
            #print "id_list", id_list
            return [self._load(_id, loaded_dict) for _id in id_list]


    def _load(self, _id, loaded_dict):
        if _id not in loaded_dict:
            #try:
                #print datetime.datetime.now(), "690: self.db[_id]"
                loaded_dict[_id] = self.db[_id]
            #except:
            #    print "problem:", _id
            #    raise

        doc = loaded_dict[_id]

        #print _id, doc, loaded_dict

        obj = self._obj_by_id.get(_id, None)
        if obj is None or getattr(obj, '_rev', None) != doc['_rev']:
            #print obj is None or getattr(obj, '_id', 'no id'), obj is None or getattr(obj, '_rev', 'no rev'), doc['_rev']
            #print self._obj_by_id.items()
            obj = self._unpack(doc, doc, loaded_dict, obj)

        base_cls, func_tuple = findHandler(type(obj), _couchable_types)
        if func_tuple:
            func_tuple[1](obj, self)

        obj._cdb = self

        return obj


# Docs
_couchable_types = collections.OrderedDict()
def registerDocType(type_, preStore_func=(lambda obj, cdb: None), postLoad_func=(lambda obj, cdb: None)):
    """
    @type  type_: type
    @param type_: Instances of this type will be stored as top-level CouchDB documents.
    @type  preStore_func: callable
    @param preStore_func: A callback of the form C{lambda obj, cdb: None}, called just before storing the object.
    @type  postLoad_func: callable
    @param postLoad_func: A callback of the form C{lambda obj, cdb: None}, called just after loading the object.
    @rtype: type
    @return: The C{type_} parameter.

    Example: C{registerDocType(CouchableDoc, lambda obj, cdb: obj.preStore(cdb), lambda obj, cdb: obj.postLoad(cdb))}
    """
    _couchable_types[type_] = (preStore_func, postLoad_func)
    _couchable_types[typestr(type_)] = (preStore_func, postLoad_func)

    return type_

class CouchableDoc(object):
    """
    Base class for types that should be stored as CouchDB documents.

    Note: Deriving from this class is optional; classes may also use L{registerDocType}.
    The only advantage to subclassing this is that L{registerDocType} has already
    been called for this class.
    """
    def preStore(self, cdb):
        """
        Basic hook point for adding behavior needed just prior to storage.

        Defaults to a no-op.

        @type  cdb: CouchableDb
        @param cdb: CouchableDb object that this object is about to be stored with.
        """
        pass

    def postLoad(self, cdb):
        """
        Basic hook point for adding behavior needed just after to loading.

        Defaults to a no-op.

        @type  cdb: CouchableDb
        @param cdb: CouchableDb object that this object was just loaded from.
        """
        pass

registerDocType(CouchableDoc, lambda obj, cdb: obj.preStore(cdb), lambda obj, cdb: obj.postLoad(cdb))

def newid(obj, id_func, noUuid=False, noType=False, sep=':'):
    """
    Helper function to make document IDs more readable.

    By default, CouchableDb document IDs have the following form:

    C{module.Class:UUID}

    The intent is that each document ID will be reasonably easy to read and
    identify at a glance.  However, for some document classes, there is a more
    appropriate way to label each instance.  For example, a C{Person} class
    might want to include first and last name as part of the ID, so that casual
    examination of the document ID makes it clear which person that ID
    corresponds to.

    C{newid} has no return data; it I{sets the _id on the object} if one is not already present.

    @type  obj: object
    @param obj: The object to potentially set an C{_id} on.
    @type  id_func: callable
    @param id_func: A callable that takes the object and returns a string to include in the C{_id}.
    @type  noUuid: bool
    @param noUuid: A flag that indicates if a UUID should be appended to the ID.
    @type  noType: bool
    @param noType: A flag that indicates if type information should be prepended to the ID.
    @type  sep: string
    @param sep: The string join the various ID components with.  Defaults to C{':'}.

    Example::
        class ClassA(object):
            def __init__(self, name):
                self.name = name
            # ...
        couchable.registerDocType(ClassA,
                lambda obj, cdb: couchable.newid(obj, lambda x: x.name),
                lambda obj, cdb: None)

        couchable.newid(ClassA('foo')) == 'example.ClassA:foo:4094b428-5b45-44fe-bd27-dcb173ec98e8'
    """
    if not hasattr(obj, '_id'):
        id_list = []

        if not noType:
            id_list.append(typestr(obj))

        id_list.append(str(id_func(obj)))

        if not noUuid:
            id_list.append(str(uuid.uuid4()))

        obj._id = sep.join(id_list)

# Attachments
def doGzip(data):
    """
    Helper function for compressing byte strings.

    @type  data: byte string
    @param data: The data to compress.
    @rtype: byte string
    @return: The compressed byte string.
    """
    str_io = cStringIO.StringIO()
    gz_file = gzip.GzipFile(mode='wb', fileobj=str_io)
    gz_file.write(data)
    gz_file.close()
    return str_io.getvalue()

def doGunzip(data):
    """
    Helper function for compressing byte strings.

    @type  data: byte string
    @param data: The data to uncompress.
    @rtype: byte string
    @return: The uncompressed byte string.
    """
    str_io = cStringIO.StringIO(data)
    gz_file = gzip.GzipFile(mode='rb', fileobj=str_io)
    return gz_file.read()

_attachment_handlers = collections.OrderedDict()
def registerAttachmentType(type_,
        serialize_func=(lambda obj: pickle.dumps(obj)),
        deserialize_func=(lambda data: pickle.loads(data)),
        content_type='application/octet-stream', gzip=False):
    """
    @type  type_: type
    @param type_: Instances of this type will be stored as attachments instead of CouchDB documents.
    @type  serialize_func: callable
    @param serialize_func: A callback of the form C{lambda obj: pickle.dumps(obj)}, called before attaching the object.  The callable needs to accept the object and return a byte string.
    @type  deserialize_func: callable
    @param deserialize_func: A callback of the form C{lambda data: pickle.loads(data)}, called after retreiving the attached object.  The callable needs to accept a byte string and return the object.
    @type  content_type: str
    @param content_type: The content type of the attached objected (C{'application/octet-stream'}, etc.).
    @type  gzip: bool
    @param gzip: Indiates if the byte string should be compressed or not.
    @rtype: type
    @return: The C{type_} parameter.

    Example::
        registerAttachmentType(CouchableAttachment,
            lambda obj: CouchableAttachment.pack(obj),
            lambda data: CouchableAttachment.unpack(data),
            'application/octet-stream')
    """
    if gzip:
        handler_tuple = (lambda data: doGzip(serialize_func(data)), lambda data: deserialize_func(doGunzip(data)), content_type)
    else:
        handler_tuple = (serialize_func, deserialize_func, content_type)

    _packer(type_)(CouchableDb._pack_attachment)
    _attachment_handlers[type_] = handler_tuple
    _attachment_handlers[typestr(type_)] = handler_tuple

    return type_

class CouchableAttachment(object):
    """
    Base class for types that should be stored as CouchDB attachments.

    Note: Deriving from this class is optional; classes may also use L{registerAttachmentType}.
    The only advantage to subclassing this class is that L{registerAttachmentType} has already
    been called for this class.
    """

    @staticmethod
    def pack(obj):
        """
        C{@staticmethod} hook point for serializing the attachment class.

        Defaults to C{pickle.dumps(obj)}.

        @type  obj: object
        @param obj: The object to serialize and upload as an attachment.
        @rtype: byte string
        @return: The serialized data.
        """
        return pickle.dumps(obj)

    @staticmethod
    def unpack(data):
        """
        C{@staticmethod} hook point for deserializing the attachment class.

        Defaults to C{pickle.dumps(obj)}.

        @type  data: byte string
        @param data: The serlized data to unserialize into an object.
        @rtype: object
        @return: The unserialized object.
        """
        return pickle.loads(data)

registerAttachmentType(CouchableAttachment,
        lambda obj: CouchableAttachment.pack(obj),
        lambda data: CouchableAttachment.unpack(data),
        'application/octet-stream')

# eof
