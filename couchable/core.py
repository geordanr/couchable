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
import itertools
import os
import pprint
import re
import subprocess
import sys
import tempfile
import time
import uuid
import weakref

#import yaml
import couchdb
import couchdb.client
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
_pack_visitors = collections.OrderedDict()
def _packer(*args):
    def func(func_):
        for type_ in args:
            packer(type_, func_)
        return func_
    return func

def packer(type_, func_):
    _pack_visitors[type_] = func_
    _pack_visitors[typestr(type_)] = func_

_unpack_visitors = collections.OrderedDict()
def _unpacker(*args):
    def func(func_):
        for type_ in args:
            unpacker(type_, func_)
        return func_
    return func

def unpacker(type_, func_):
    _unpack_visitors[type_] = func_
    _unpack_visitors[typestr(type_)] = func_



# function for navigating the above dics of visitors, etc.
def findVisitor(cls_or_name, visitor_dict):
    """
    >>> class A(object): pass
    ... 
    >>> class B(A): pass
    ... 
    >>> class C(object): pass
    ... 
    >>> visitors={A:'AAA'}
    >>> findVisitor(A, visitors)
    (<class 'couchable.core.A'>, 'AAA')
    >>> findVisitor(B, visitors)
    (<class 'couchable.core.A'>, 'AAA')
    >>> findVisitor(C, visitors)
    (None, None)
    """
    #if isinstance(cls_or_name, basestring):
    #    for type_, visitor in reversed(visitor_dict.items()):
    #        if cls_or_name == str(type_):
    #            return type_, visitor
    #el
    if cls_or_name in visitor_dict:
        return cls_or_name, visitor_dict[cls_or_name]
    else:
        for type_, visitor in reversed(visitor_dict.items()):
            if isinstance(type_, type) and issubclass(cls_or_name, type_):
                return type_, visitor
    
    return None, None

class CouchableDb(object):
    _wrapper_cache = weakref.WeakValueDictionary()
    
    def __init__(self, db):
        assert db not in self._wrapper_cache
        
        self._wrapper_cache[db] = self
        self.db = db
        self._obj_by_id = weakref.WeakValueDictionary()
        
    
    def store(self, what):
        if not isinstance(what, list):
            store_list = [what]
        else:
            store_list = what
            
        self._done_dict = collections.OrderedDict()
        
        for obj in store_list:
            self._store(obj)
        
        # Actually (finally) send the data to couchdb.
        try:
            #pprint.pprint([(x[0]._id, getattr(x[0], '_rev', None)) for x in self._done_dict.values()])
            ret_list = self.db.update([x[1] for x in self._done_dict.values()])
        except:
            #print self._done_dict.values()
            raise
        
        for ret, store_tuple in itertools.izip(ret_list, self._done_dict.values()):
            success, _id, _rev = ret
            obj, doc, attachment_list = store_tuple
            if success:
                for content, content_name, content_type in attachment_list:
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
            
        base_cls, func_tuple = findVisitor(type(obj), _couchable_types)
        if func_tuple:
            func_tuple[0](obj, self)

        if not hasattr(obj, '_id'):
            obj._id = '{}:{}'.format(typestr(obj), uuid.uuid4()).lstrip('_')
            assert obj._id not in self._obj_by_id
            
        if obj._id not in self._done_dict:
            self._done_dict[obj._id] = (obj, {}, [])
                
            attachment_list = []
            # This code matches the code in _pack_object
            doc = self._obj2doc_empty(obj)
            doc.update(self._pack_dict_keyMeansObject(doc, obj.__dict__, attachment_list, '', True))
            #self._pack(doc, obj, attachment_list)
                
            self._done_dict[obj._id] = (obj, doc, attachment_list)
        

    def _pack(self, parent_doc, data, attachment_list, name, isKey=False):
        cls = type(data)
        
        base_cls, visitor = findVisitor(cls, _pack_visitors)
        
        if visitor:
            return visitor(self, parent_doc, data, attachment_list, name, isKey)
        else:
            raise UncouchableException("No _packer for type", cls, data)
        
        #if cls in _pack_visitors:
        #    return _pack_visitors[cls](self, parent_doc, data, attachment_list, name, isKey)
        #else:
        #    for types, func in reversed(_pack_visitors.items()):
        #        if isinstance(data, types):
        #            return func(self, parent_doc, data, attachment_list, name, isKey)
        #            break
        #    else:
        #        raise UncouchableException("No _packer for type", cls, data)
                
    def _obj2doc_empty(self, data):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
        >>> obj = object()
        >>> pprint.pprint(cdb._obj2doc_empty(obj))
        {'couchable:': {'class': 'object', 'module': '__builtin__'}}
        """
        cls = type(data)
        doc = {FIELD_NAME:{'class': cls.__name__}}
       
        if hasattr(cls, '__module__'):
            doc[FIELD_NAME]['module'] = str(cls.__module__)
        return doc
    
    def _obj2doc_consargs(self, data, args=None, kwargs=None):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
        >>> obj = tuple([1, 2, 3])
        >>> pprint.pprint(cdb._obj2doc_consargs(obj, list(obj), {}))
        {'couchable:': {'args': [1, 2, 3],
                        'class': 'tuple',
                        'kwargs': {},
                        'module': '__builtin__'}}
        """
        doc = self._obj2doc_empty(data)
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
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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
        base_cls, callback_tuple = findVisitor(cls, _couchable_types)
        
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
            else:
                # This code matches the code in _store
                doc = self._obj2doc_empty(data)
                doc.update(self._pack_dict_keyMeansObject(parent_doc, data.__dict__, attachment_list, name, True))
                
                return doc
        
    @_packer(str, unicode)
    def _pack_native(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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

    @_packer(int, long, float)
    def _pack_native_keyAsRepr(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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
        
    @_packer(tuple, frozenset)
    def _pack_consargs_keyAsKey(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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
            return self._obj2doc_consargs(data, [self._pack_list_noKey(parent_doc, list(data), attachment_list, name, False)])
        
    @_packer(list)
    def _pack_list_noKey(self, parent_doc, data, attachment_list, name, isKey):
        """
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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
        >>> cdb=CouchableDb(couchdb.Server()['testing'])
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
            private_keys = {k for k in data.keys() if k.startswith('_') and k not in ('_id', '_rev', '_attachments')}
        else:
            private_keys = set()
            
        doc = {self._pack(parent_doc, k, attachment_list, '{}>{}'.format(name, str(k)), True):
            self._pack(parent_doc, v, attachment_list, '{}.{}'.format(name, str(k)), False)
            for k,v in data.items() if k not in private_keys and k != '_attachments'}
            
        if private_keys:
            doc.setdefault(FIELD_NAME, {})
            doc[FIELD_NAME]['private'] = {self._pack(parent_doc, k, attachment_list, '{}>{}'.format(name, str(k)), True):
                self._pack(parent_doc, v, attachment_list, '{}.{}'.format(name, str(k)), False)
                for k,v in data.items() if k in private_keys}

        return doc

    def _pack_attachment(self, parent_doc, data, attachment_list, name, isKey):
        cls = type(data)
        
        base_cls, visitor_tuple = findVisitor(cls, _attachment_visitors)
        
        content = visitor_tuple[0](data)
        
        attachment_list.append((content, name, visitor_tuple[2]))
        return '{}{}:{}:{}'.format(FIELD_NAME, 'attachment', typestr(base_cls), name)

    def _unpack(self, parent_doc, doc, loaded_dict, inst=None):
        try:
            if isinstance(doc, (str, unicode)):
                if doc.startswith(FIELD_NAME):
                    _, method_str, data = doc.split(':', 2)
                    
                    if method_str == 'id':
                        return self._load(data, loaded_dict)
                    
                    type_str, data = data.split(':', 1)
                    if method_str == 'append':
                        if type_str == 'unicode':
                            return unicode(data, 'utf8')
                        if type_str == 'str':
                            return data
                    
                    elif method_str == 'repr':
                        if type_str in __builtins__:
                            return __builtins__.get(type_str)(data)
                        else:
                            return importstr(*type_str.rsplit('.', 1))(data)
    
                    elif method_str == 'key':
                        return self._unpack(parent_doc, parent_doc[FIELD_NAME]['keys'][doc], loaded_dict)
    
                    elif method_str == 'attachment':
                        base_cls, visitor_tuple = findVisitor(type_str, _attachment_visitors)
                        
                        if base_cls is None:
                            # FIXME: error?
                            print type_str, data, _attachment_visitors
                        
                        attachment_response = self.db.get_attachment(parent_doc, data)
                        return visitor_tuple[1](attachment_response.read())
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
                            self._obj_by_id[doc['_id']] = inst
                        
                        #print "unpack isinstance(doc, dict) doc:", doc.get('_id', 'still no id')
                        #print "unpack isinstance(doc, dict) doc:", doc.get('_rev', 'still no rev')
                        
                        inst.__dict__.update(info.get('private', {}))
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
        id_list = []
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
                
                if len(item) > 1:
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
        
        base_cls, func_tuple = findVisitor(type(obj), _couchable_types)
        if func_tuple:
            func_tuple[1](obj, self)
            
        return obj


# Docs
_couchable_types = collections.OrderedDict()
def registerDocType(type_, preStore_func=lambda x, cdb: None, postLoad_func=lambda x, cdb: None):
    """
    Example: registerDocType(CouchableDoc, lambda obj: obj.preStore(), lambda obj: obj.postLoad())
    """
    _couchable_types[type_] = (preStore_func, postLoad_func)
    _couchable_types[typestr(type_)] = (preStore_func, postLoad_func)
    
    return type_

class CouchableDoc(object):
    """
    Base class for couchable python objects.  Note: Deriving from this class is optional; classes may also use registerDocType(...).
    """
    def preStore(self):
        pass
    
    def postLoad(self):
        pass
    
registerDocType(CouchableDoc, lambda obj, cdb: obj.preStore(), lambda obj, cdb: obj.postLoad())

def newid(obj, id_func, noUuid=False, noType=False, sep=':'):
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
    str_io = cStringIO.StringIO()
    gz_file = gzip.GzipFile(mode='wb', fileobj=str_io)
    gz_file.write(data)
    gz_file.close()
    return str_io.getvalue()

def doGunzip(data):
    str_io = cStringIO.StringIO(data)
    gz_file = gzip.GzipFile(mode='rb', fileobj=str_io)
    return gz_file.read()
    
_attachment_visitors = collections.OrderedDict()
def registerAttachmentType(type_, pack_func, unpack_func, content_type, gzip=False):
    """
    Example: registerAttachmentType(CouchableAttachment, \
            lambda obj: CouchableAttachment.pack(obj), \
            lambda data: CouchableAttachment.unpack(data), \
            'application/octet-stream')
    """
    if gzip:
        visitor_tuple = (lambda data: doGzip(pack_func(data)), lambda data: unpack_func(doGunzip(data)), content_type)
    else:
        visitor_tuple = (pack_func, unpack_func, content_type)

    _packer(type_)(CouchableDb._pack_attachment)
    _attachment_visitors[type_] = visitor_tuple
    _attachment_visitors[typestr(type_)] = visitor_tuple

class CouchableAttachment(object):
    """
    Base class for attachment python objects.  Note: Deriving from this class is optional; classes may also use registerAttachmentType(...).
    """
    
    @staticmethod
    def pack(obj):
        return pickle.dumps(obj)
    
    @staticmethod
    def unpack(data):
        return pickle.loads(data)

registerAttachmentType(CouchableAttachment,
        lambda obj: CouchableAttachment.pack(obj),
        lambda data: CouchableAttachment.unpack(data),
        'application/octet-stream')
    
# eof