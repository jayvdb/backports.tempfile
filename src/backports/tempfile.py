"""
Partial backport of Python 3.5's tempfile module:

    _infer_return_type
    _sanitize_params
    _TemporaryFileCloser
    _TemporaryFileWrapper
    NamedTemporaryFile
    TemporaryDirectory

Backport modifications are marked with marked with "XXX backport".
"""
from __future__ import absolute_import

import io as _io
import os as _os
import functools as _functools
import sys
import warnings as _warnings
from shutil import rmtree as _rmtree

from backports.weakref import finalize
# Do not use backports.os fsencode as it is too unreliable on
# all OS at the moment.  Better for this tempdir backport to
# clearly not support complex filenames until the os backport
# is more mature, except where the native os module provides
# the necessary support.
try:
    from os import fsencode as _fsencode
except ImportError:
    _fsencode = None

from tempfile import (
    gettempdir as gettempdirb,
    _get_candidate_names,
    _text_openflags,
    _bin_openflags,
    TMP_MAX,
)

if sys.version_info[0] == 3:
    _PY3 = True
else:
    _PY3 = False

if _PY3:
    unicode = str

"""
_text_openflags = _os.O_RDWR | _os.O_CREAT | _os.O_EXCL
if hasattr(_os, 'O_NOFOLLOW'):
    _text_openflags |= _os.O_NOFOLLOW

_bin_openflags = _text_openflags
if hasattr(_os, 'O_BINARY'):
    _bin_openflags |= _os.O_BINARY

if hasattr(_os, 'TMP_MAX'):
    TMP_MAX = _os.TMP_MAX
else:
    TMP_MAX = 1000
"""

# This variable _was_ unused for legacy reasons, see issue 10354.
# But as of 3.5 we actually use it at runtime so changing it would
# have a possibly desirable side effect...  But we do not want to support
# that as an API.  It is undocumented on purpose.  Do not depend on this.
template = u"tmp"


def gettempdir():
    tempdir_ = gettempdirb()
    # XXX very basic approach for returning unicode
    try:
        return unicode(tempdir_)
    except:
        return tempdir_


def _infer_return_type(*args):
    """Look at the type of all args and divine their implied return type."""
    return_type = None
    for arg in args:
        if arg is None:
            continue
        if isinstance(arg, bytes):
            if return_type and return_type is not bytes:
                raise TypeError("Can't mix bytes and non-bytes in "
                                "path components.")
            return_type = bytes
        else:
            if return_type is bytes:
                raise TypeError("Can't mix bytes and non-bytes in "
                                "path components.")
            return_type = unicode
    if return_type is None:
        return unicode  # tempfile APIs return a unicode by default.
    return return_type


def _sanitize_params(prefix, suffix, dir):
    """Common parameter processing for most APIs in this module."""
    output_type = _infer_return_type(prefix, suffix, dir)
    if suffix is None:
        suffix = output_type()
    if prefix is None:
        if output_type is unicode:
            prefix = template
        else:
            prefix = bytes(template)
    if dir is None:
        if output_type is unicode:
            dir = gettempdir()
        else:
            dir = gettempdirb()
    return prefix, suffix, dir, output_type


def _mkstemp_inner(dir, pre, suf, flags, output_type):
    """Code common to mkstemp, TemporaryFile, and NamedTemporaryFile."""

    names = _get_candidate_names()
    # Using backports.os.fsencode here fails badly on all platforms,
    # going into an infinite loop.
    if _fsencode and output_type is bytes:
        names = map(_fsencode, names)

    for seq in range(TMP_MAX):
        name = next(names)
        file = _os.path.join(dir, pre + name + suf)
        try:
            fd = _os.open(file, flags, 0o600)
        except FileExistsError:
            continue    # try again
        except PermissionError:
            # This exception is thrown when a directory with the chosen name
            # already exists on windows.
            if (_os.name == 'nt' and _os.path.isdir(dir) and
                _os.access(dir, _os.W_OK)):
                continue
            else:
                raise
        return (fd, _os.path.abspath(file))

    raise FileExistsError(_errno.EEXIST,
                          "No usable temporary file name found")


# XXX backport:  Rather than backporting all of mkdtemp(), we just create a
# thin wrapper implementing its Python 3.5 signature.
if sys.version_info < (3, 5):
    from tempfile import mkdtemp as old_mkdtemp

    def mkdtemp(suffix=None, prefix=None, dir=None):
        """
        Wrap `tempfile.mkdtemp()` to make the suffix and prefix optional (like Python 3.5).
        """
        kwargs = {k: v for (k, v) in
                  dict(suffix=suffix, prefix=prefix, dir=dir).items()
                  if v is not None}
        return old_mkdtemp(**kwargs)

else:
    from tempfile import mkdtemp


# XXX backport: ResourceWarning was added in Python 3.2.
# For earlier versions, fall back to RuntimeWarning instead.
_ResourceWarning = RuntimeWarning if sys.version_info < (3, 2) else ResourceWarning


class _TemporaryFileCloser:
    """A separate object allowing proper closing of a temporary file's
    underlying file object, without adding a __del__ method to the
    temporary file."""

    file = None  # Set here since __del__ checks it
    close_called = False

    def __init__(self, file, name, delete=True):
        self.file = file
        self.name = name
        self.delete = delete

    # NT provides delete-on-close as a primitive, so we don't need
    # the wrapper to do anything special.  We still use it so that
    # file.name is useful (i.e. not "(fdopen)") with NamedTemporaryFile.
    if _os.name != 'nt':
        # Cache the unlinker so we don't get spurious errors at
        # shutdown when the module-level "os" is None'd out.  Note
        # that this must be referenced as self.unlink, because the
        # name TemporaryFileWrapper may also get None'd out before
        # __del__ is called.

        def close(self, unlink=_os.unlink):
            if not self.close_called and self.file is not None:
                self.close_called = True
                try:
                    self.file.close()
                finally:
                    if self.delete:
                        unlink(self.name)

        # Need to ensure the file is deleted on __del__
        def __del__(self):
            self.close()

    else:
        def close(self):
            if not self.close_called:
                self.close_called = True
                self.file.close()


class _TemporaryFileWrapper:
    """Temporary file wrapper

    This class provides a wrapper around files opened for
    temporary use.  In particular, it seeks to automatically
    remove the file when it is no longer needed.
    """

    def __init__(self, file, name, delete=True):
        self.file = file
        self.name = name
        self.delete = delete
        self._closer = _TemporaryFileCloser(file, name, delete)

    def __getattr__(self, name):
        # Attribute lookups are delegated to the underlying file
        # and cached for non-numeric results
        # (i.e. methods are cached, closed and friends are not)
        file = self.__dict__['file']
        a = getattr(file, name)
        if hasattr(a, '__call__'):
            func = a
            @_functools.wraps(func)
            def func_wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            # Avoid closing the file as long as the wrapper is alive,
            # see issue #18879.
            func_wrapper._closer = self._closer
            a = func_wrapper
        if not isinstance(a, int):
            setattr(self, name, a)
        return a

    # The underlying __enter__ method returns the wrong object
    # (self.file) so override it to return the wrapper
    def __enter__(self):
        self.file.__enter__()
        return self

    # Need to trap __exit__ as well to ensure the file gets
    # deleted when used in a with statement
    def __exit__(self, exc, value, tb):
        result = self.file.__exit__(exc, value, tb)
        self.close()
        return result

    def close(self):
        """
        Close the temporary file, possibly deleting it.
        """
        self._closer.close()

    # iter() doesn't use __getattr__ to find the __iter__ method
    def __iter__(self):
        # Don't return iter(self.file), but yield from it to avoid closing
        # file as long as it's being used as iterator (see issue #23700).  We
        # can't use 'yield from' here because iter(file) returns the file
        # object itself, which has a close method, and thus the file would get
        # closed when the generator is finalized, due to PEP380 semantics.
        for line in self.file:
            yield line


def NamedTemporaryFile(mode='w+b', buffering=-1, encoding=None,
                       newline=None, suffix=None, prefix=None,
                       dir=None, delete=True):
    """Create and return a temporary file.
    Arguments:
    'prefix', 'suffix', 'dir' -- as for mkstemp.
    'mode' -- the mode argument to io.open (default "w+b").
    'buffering' -- the buffer size argument to io.open (default -1).
    'encoding' -- the encoding argument to io.open (default None)
    'newline' -- the newline argument to io.open (default None)
    'delete' -- whether the file is deleted on close (default True).
    The file is created as mkstemp() would do it.

    Returns an object with a file-like interface; the name of the file
    is accessible as its 'name' attribute.  The file will be automatically
    deleted when it is closed unless the 'delete' argument is set to False.
    """

    prefix, suffix, dir, output_type = _sanitize_params(prefix, suffix, dir)

    flags = _bin_openflags

    # Setting O_TEMPORARY in the flags causes the OS to delete
    # the file when it is closed.  This is only supported by Windows.
    if _os.name == 'nt' and delete:
        flags |= _os.O_TEMPORARY

    (fd, name) = _mkstemp_inner(dir, prefix, suffix, flags, output_type)
    try:
        file = _io.open(fd, mode, buffering=buffering,
                        newline=newline, encoding=encoding)

        return _TemporaryFileWrapper(file, name, delete)
    except BaseException:
        _os.unlink(name)
        _os.close(fd)
        raise


class TemporaryDirectory(object):
    """Create and return a temporary directory.  This has the same
    behavior as mkdtemp but can be used as a context manager.  For
    example:

        with TemporaryDirectory() as tmpdir:
            ...

    Upon exiting the context, the directory and everything contained
    in it are removed.
    """

    def __init__(self, suffix=None, prefix=None, dir=None):
        self.name = mkdtemp(suffix, prefix, dir)
        self._finalizer = finalize(
            self, self._cleanup, self.name,
            warn_message="Implicitly cleaning up {!r}".format(self))

    @classmethod
    def _cleanup(cls, name, warn_message):
        _rmtree(name)
        _warnings.warn(warn_message, _ResourceWarning)


    def __repr__(self):
        return "<{} {!r}>".format(self.__class__.__name__, self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, exc, value, tb):
        self.cleanup()

    def cleanup(self):
        if self._finalizer.detach():
            _rmtree(self.name)
