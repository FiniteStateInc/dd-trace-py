import contextlib
import sys
import os

import ddtrace
from ddtrace.constants import SPAN_MEASURED_KEY
from ddtrace.encoding import JSONEncoder, MsgpackEncoder
from ddtrace.ext import http
from ddtrace.internal.writer import AgentWriter
from ddtrace.span import Span
from ddtrace.tracer import Tracer

from tests.subprocesstest import SubprocessTestCase


class DummyWriter(AgentWriter):
    """DummyWriter is a small fake writer used for tests. not thread-safe."""

    def __init__(self, *args, **kwargs):
        # original call
        super(DummyWriter, self).__init__(*args, **kwargs)

        # dummy components
        self.spans = []
        self.traces = []
        self.services = {}
        self.json_encoder = JSONEncoder()
        self.msgpack_encoder = MsgpackEncoder()

    def write(self, spans=None, services=None):
        if spans:
            # the traces encoding expect a list of traces so we
            # put spans in a list like we do in the real execution path
            # with both encoders
            trace = [spans]
            self.json_encoder.encode_traces(trace)
            self.msgpack_encoder.encode_traces(trace)
            self.spans += spans
            self.traces += trace

        if services:
            self.json_encoder.encode_services(services)
            self.msgpack_encoder.encode_services(services)
            self.services.update(services)

    def pop(self):
        # dummy method
        s = self.spans
        self.spans = []
        return s

    def pop_traces(self):
        # dummy method
        traces = self.traces
        self.traces = []
        return traces

    def pop_services(self):
        # dummy method

        # Setting service info has been deprecated, we want to make sure nothing ever gets written here
        assert self.services == {}
        s = self.services
        self.services = {}
        return s


class DummyTracer(Tracer):
    """
    DummyTracer is a tracer which uses the DummyWriter by default
    """

    def __init__(self):
        super(DummyTracer, self).__init__()
        self._update_writer()

    def _update_writer(self):
        # Track which writer the DummyWriter was created with, used
        # some tests
        if not isinstance(self.writer, DummyWriter):
            self.original_writer = self.writer
        # LogWriters don't have an api property, so we test that
        # exists before using it to assign hostname/port
        if hasattr(self.writer, "api"):
            self.writer = DummyWriter(
                hostname=self.writer.api.hostname,
                port=self.writer.api.port,
                filters=self.writer._filters,
                priority_sampler=self.writer._priority_sampler,
            )
        else:
            self.writer = DummyWriter(
                hostname="", port=0, filters=self.writer._filters, priority_sampler=self.writer._priority_sampler,
            )

    def configure(self, *args, **kwargs):
        super(DummyTracer, self).configure(*args, **kwargs)
        # `.configure()` may reset the writer
        self._update_writer()


def assert_dict_issuperset(a, b):
    assert set(a.items()).issuperset(set(b.items())), "{a} is not a superset of {b}".format(a=a, b=b)


def assert_is_measured(span):
    """Assert that the span has the proper _dd.measured tag set"""
    assert SPAN_MEASURED_KEY in span.metrics
    assert SPAN_MEASURED_KEY not in span.meta
    assert span.get_metric(SPAN_MEASURED_KEY) == 1


def assert_is_not_measured(span):
    """Assert that the span does not set _dd.measured"""
    assert SPAN_MEASURED_KEY not in span.meta
    if SPAN_MEASURED_KEY in span.metrics:
        assert span.get_metric(SPAN_MEASURED_KEY) == 0
    else:
        assert SPAN_MEASURED_KEY not in span.metrics


def assert_span_http_status_code(span, code):
    """Assert on the span's 'http.status_code' tag"""
    tag = span.get_tag(http.STATUS_CODE)
    code = str(code)
    assert tag == code, "%r != %r" % (tag, code)


@contextlib.contextmanager
def override_env(env):
    """
    Temporarily override ``os.environ`` with provided values::

        >>> with self.override_env(dict(DATADOG_TRACE_DEBUG=True)):
            # Your test
    """
    # Copy the full original environment
    original = dict(os.environ)

    # Update based on the passed in arguments
    os.environ.update(env)
    try:
        yield
    finally:
        # Full clear the environment out and reset back to the original
        os.environ.clear()
        os.environ.update(original)


@contextlib.contextmanager
def override_global_tracer(tracer):
    """Helper functions that overrides the global tracer available in the
    `ddtrace` package. This is required because in some `httplib` tests we
    can't get easily the PIN object attached to the `HTTPConnection` to
    replace the used tracer with a dummy tracer.
    """
    original_tracer = ddtrace.tracer
    ddtrace.tracer = tracer
    yield
    ddtrace.tracer = original_tracer


class BaseTestCase(SubprocessTestCase):
    """
    BaseTestCase extends ``unittest.TestCase`` to provide some useful helpers/assertions


    Example::

        from tests import BaseTestCase


        class MyTestCase(BaseTestCase):
            def test_case(self):
                with self.override_config('flask', dict(distributed_tracing_enabled=True):
                    pass
    """

    # Expose `override_env` as `self.override_env`
    override_env = staticmethod(override_env)

    assert_is_measured = staticmethod(assert_is_measured)

    assert_is_not_measured = staticmethod(assert_is_not_measured)

    @staticmethod
    @contextlib.contextmanager
    def override_global_config(values):
        """
        Temporarily override an global configuration::

            >>> with self.override_global_config(dict(name=value,...)):
                # Your test
        """
        # List of global variables we allow overriding
        # DEV: We do not do `ddtrace.config.keys()` because we have all of our integrations
        global_config_keys = [
            "analytics_enabled",
            "report_hostname",
            "health_metrics_enabled",
            "env",
            "version",
            "service",
        ]

        # Grab the current values of all keys
        originals = dict((key, getattr(ddtrace.config, key)) for key in global_config_keys)

        # Override from the passed in keys
        for key, value in values.items():
            if key in global_config_keys:
                setattr(ddtrace.config, key, value)
        try:
            yield
        finally:
            # Reset all to their original values
            for key, value in originals.items():
                setattr(ddtrace.config, key, value)

    @staticmethod
    @contextlib.contextmanager
    def override_config(integration, values):
        """
        Temporarily override an integration configuration value::

            >>> with self.override_config('flask', dict(service_name='test-service')):
                # Your test
        """
        options = getattr(ddtrace.config, integration)

        original = dict((key, options.get(key)) for key in values.keys())

        options.update(values)
        try:
            yield
        finally:
            options.update(original)

    @staticmethod
    @contextlib.contextmanager
    def override_http_config(integration, values):
        """
        Temporarily override an integration configuration for HTTP value::

            >>> with self.override_http_config('flask', dict(trace_query_string=True)):
                # Your test
        """
        options = getattr(ddtrace.config, integration).http

        original = {}
        for key, value in values.items():
            original[key] = getattr(options, key)
            setattr(options, key, value)

        try:
            yield
        finally:
            for key, value in original.items():
                setattr(options, key, value)

    @staticmethod
    @contextlib.contextmanager
    def override_sys_modules(modules):
        """
        Temporarily override ``sys.modules`` with provided dictionary of modules::

            >>> mock_module = mock.MagicMock()
            >>> mock_module.fn.side_effect = lambda: 'test'
            >>> with self.override_sys_modules(dict(A=mock_module)):
                # Your test
        """
        original = dict(sys.modules)

        sys.modules.update(modules)
        try:
            yield
        finally:
            sys.modules.clear()
            sys.modules.update(original)


NO_CHILDREN = object()


class TestSpan(Span):
    """
    Test wrapper for a :class:`ddtrace.span.Span` that provides additional functions and assertions

    Example::

        span = tracer.trace('my.span')
        span = TestSpan(span)

        if span.matches(name='my.span'):
            print('matches')

        # Raises an AssertionError
        span.assert_matches(name='not.my.span', meta={'system.pid': getpid()})
    """

    def __init__(self, span):
        """
        Constructor for TestSpan

        :param span: The :class:`ddtrace.span.Span` to wrap
        :type span: :class:`ddtrace.span.Span`
        """
        if isinstance(span, TestSpan):
            span = span._span

        # DEV: Use `object.__setattr__` to by-pass this class's `__setattr__`
        object.__setattr__(self, "_span", span)

    def __getattr__(self, key):
        """
        First look for property on the base :class:`ddtrace.span.Span` otherwise return this object's attribute
        """
        if hasattr(self._span, key):
            return getattr(self._span, key)

        return self.__getattribute__(key)

    def __setattr__(self, key, value):
        """Pass through all assignment to the base :class:`ddtrace.span.Span`"""
        return setattr(self._span, key, value)

    def __eq__(self, other):
        """
        Custom equality code to ensure we are using the base :class:`ddtrace.span.Span.__eq__`

        :param other: The object to check equality with
        :type other: object
        :returns: True if equal, False otherwise
        :rtype: bool
        """
        if isinstance(other, TestSpan):
            return other._span == self._span
        elif isinstance(other, Span):
            return other == self._span
        return other == self

    def matches(self, **kwargs):
        """
        Helper function to check if this span's properties matches the expected.

        Example::

            span = TestSpan(span)
            span.matches(name='my.span', resource='GET /')

        :param kwargs: Property/Value pairs to evaluate on this span
        :type kwargs: dict
        :returns: True if the arguments passed match, False otherwise
        :rtype: bool
        """
        for name, value in kwargs.items():
            # Special case for `meta`
            if name == "meta" and not self.meta_matches(value):
                return False

            # Ensure it has the property first
            if not hasattr(self, name):
                return False

            # Ensure the values match
            if getattr(self, name) != value:
                return False

        return True

    def meta_matches(self, meta, exact=False):
        """
        Helper function to check if this span's meta matches the expected

        Example::

            span = TestSpan(span)
            span.meta_matches({'system.pid': getpid()})

        :param meta: Property/Value pairs to evaluate on this span
        :type meta: dict
        :param exact: Whether to do an exact match on the meta values or not, default: False
        :type exact: bool
        :returns: True if the arguments passed match, False otherwise
        :rtype: bool
        """
        if exact:
            return self.meta == meta

        for key, value in meta.items():
            if key not in self.meta:
                return False
            if self.meta[key] != value:
                return False
        return True

    def assert_matches(self, **kwargs):
        """
        Assertion method to ensure this span's properties match as expected

        Example::

            span = TestSpan(span)
            span.assert_matches(name='my.span')

        :param kwargs: Property/Value pairs to evaluate on this span
        :type kwargs: dict
        :raises: AssertionError
        """
        for name, value in kwargs.items():
            # Special case for `meta`
            if name == "meta":
                self.assert_meta(value)
            elif name == "metrics":
                self.assert_metrics(value)
            else:
                assert hasattr(self, name), "{0!r} does not have property {1!r}".format(self, name)
                assert getattr(self, name) == value, "{0!r} property {1}: {2!r} != {3!r}".format(
                    self, name, getattr(self, name), value
                )

    def assert_meta(self, meta, exact=False):
        """
        Assertion method to ensure this span's meta match as expected

        Example::

            span = TestSpan(span)
            span.assert_meta({'system.pid': getpid()})

        :param meta: Property/Value pairs to evaluate on this span
        :type meta: dict
        :param exact: Whether to do an exact match on the meta values or not, default: False
        :type exact: bool
        :raises: AssertionError
        """
        if exact:
            assert self.meta == meta
        else:
            for key, value in meta.items():
                assert key in self.meta, "{0} meta does not have property {1!r}".format(self, key)
                assert self.meta[key] == value, "{0} meta property {1!r}: {2!r} != {3!r}".format(
                    self, key, self.meta[key], value
                )

    def assert_metrics(self, metrics, exact=False):
        """
        Assertion method to ensure this span's metrics match as expected

        Example::

            span = TestSpan(span)
            span.assert_metrics({'_dd1.sr.eausr': 1})

        :param metrics: Property/Value pairs to evaluate on this span
        :type metrics: dict
        :param exact: Whether to do an exact match on the metrics values or not, default: False
        :type exact: bool
        :raises: AssertionError
        """
        if exact:
            assert self.metrics == metrics
        else:
            for key, value in metrics.items():
                assert key in self.metrics, "{0} metrics does not have property {1!r}".format(self, key)
                assert self.metrics[key] == value, "{0} metrics property {1!r}: {2!r} != {3!r}".format(
                    self, key, self.metrics[key], value
                )


class TestSpanContainer(object):
    """
    Helper class for a container of Spans.

    Subclasses of this class must implement a `get_spans` method::

        def get_spans(self):
            return []

    This class provides methods and assertions over a list of spans::

        class TestCases(BaseTracerTestCase):
            def test_spans(self):
                # TODO: Create spans

                self.assert_has_spans()
                self.assert_span_count(3)
                self.assert_structure( ... )

                # Grab only the `requests.request` spans
                spans = self.filter_spans(name='requests.request')
    """

    def _ensure_test_spans(self, spans):
        """
        internal helper to ensure the list of spans are all :class:`tests.utils.span.TestSpan`

        :param spans: List of :class:`ddtrace.span.Span` or :class:`tests.utils.span.TestSpan`
        :type spans: list
        :returns: A list og :class:`tests.utils.span.TestSpan`
        :rtype: list
        """
        return [span if isinstance(span, TestSpan) else TestSpan(span) for span in spans]

    @property
    def spans(self):
        return self._ensure_test_spans(self.get_spans())

    def get_spans(self):
        """subclass required property"""
        raise NotImplementedError

    def _build_tree(self, root):
        """helper to build a tree structure for the provided root span"""
        children = []
        for span in self.spans:
            if span.parent_id == root.span_id:
                children.append(self._build_tree(span))

        return TestSpanNode(root, children)

    def get_root_span(self):
        """
        Helper to get the root span from the list of spans in this container

        :returns: The root span if one was found, None if not, and AssertionError if multiple roots were found
        :rtype: :class:`tests.utils.span.TestSpanNode`, None
        :raises: AssertionError
        """
        root = None
        for span in self.spans:
            if span.parent_id is None:
                if root is not None:
                    raise AssertionError("Multiple root spans found {0!r} {1!r}".format(root, span))
                root = span

        assert root, "No root span found in {0!r}".format(self.spans)

        return self._build_tree(root)

    def get_root_spans(self):
        """
        Helper to get all root spans from the list of spans in this container

        :returns: The root spans if any were found, None if not
        :rtype: list of :class:`tests.utils.span.TestSpanNode`, None
        """
        roots = []
        for span in self.spans:
            if span.parent_id is None:
                roots.append(self._build_tree(span))

        return sorted(roots, key=lambda s: s.start)

    def assert_trace_count(self, count):
        """Assert the number of unique trace ids this container has"""
        trace_count = len(self.get_root_spans())
        assert trace_count == count, "Trace count {0} != {1}".format(trace_count, count)

    def assert_span_count(self, count):
        """Assert this container has the expected number of spans"""
        assert len(self.spans) == count, "Span count {0} != {1}".format(len(self.spans), count)

    def assert_has_spans(self):
        """Assert this container has spans"""
        assert len(self.spans), "No spans found"

    def assert_has_no_spans(self):
        """Assert this container does not have any spans"""
        assert len(self.spans) == 0, "Span count {0}".format(len(self.spans))

    def filter_spans(self, *args, **kwargs):
        """
        Helper to filter current spans by provided parameters.

        This function will yield all spans whose `TestSpan.matches` function return `True`.

        :param args: Positional arguments to pass to :meth:`tests.utils.span.TestSpan.matches`
        :type args: list
        :param kwargs: Keyword arguments to pass to :meth:`tests.utils.span.TestSpan.matches`
        :type kwargs: dict
        :returns: generator for the matched :class:`tests.utils.span.TestSpan`
        :rtype: generator
        """
        for span in self.spans:
            # ensure we have a TestSpan
            if not isinstance(span, TestSpan):
                span = TestSpan(span)

            if span.matches(*args, **kwargs):
                yield span

    def find_span(self, *args, **kwargs):
        """
        Find a single span matches the provided filter parameters.

        This function will find the first span whose `TestSpan.matches` function return `True`.

        :param args: Positional arguments to pass to :meth:`tests.utils.span.TestSpan.matches`
        :type args: list
        :param kwargs: Keyword arguments to pass to :meth:`tests.utils.span.TestSpan.matches`
        :type kwargs: dict
        :returns: The first matching span
        :rtype: :class:`tests.utils.span.TestSpan`
        """
        span = next(self.filter_spans(*args, **kwargs), None)
        assert span is not None, "No span found for filter {0!r} {1!r}, have {2} spans".format(
            args, kwargs, len(self.spans)
        )
        return span


class TracerSpanContainer(TestSpanContainer):
    """
    A class to wrap a :class:`tests.utils.tracer.DummyTracer` with a
    :class:`tests.utils.span.TestSpanContainer` to use in tests
    """

    def __init__(self, tracer):
        self.tracer = tracer
        super(TracerSpanContainer, self).__init__()

    def get_spans(self):
        """
        Overridden method to return all spans attached to this tracer

        :returns: List of spans attached to this tracer
        :rtype: list
        """
        return self.tracer.writer.spans

    def reset(self):
        """Helper to reset the existing list of spans created"""
        self.tracer.writer.pop()


class TestSpanNode(TestSpan, TestSpanContainer):
    """
    A :class:`tests.utils.span.TestSpan` which is used as part of a span tree.

    Each :class:`tests.utils.span.TestSpanNode` represents the current :class:`ddtrace.span.Span`
    along with any children who have that span as it's parent.

    This class can be used to assert on the parent/child relationships between spans.

    Example::

        class TestCase(BaseTestCase):
            def test_case(self):
                # TODO: Create spans

                self.assert_structure( ... )

                tree = self.get_root_span()

                # Find the first child of the root span with the matching name
                request = tree.find_span(name='requests.request')

                # Assert the parent/child relationship of this `request` span
                request.assert_structure( ... )
    """

    def __init__(self, root, children=None):
        super(TestSpanNode, self).__init__(root)
        object.__setattr__(self, "_children", children or [])

    def get_spans(self):
        """required subclass property, returns this spans children"""
        return self._children

    def assert_structure(self, root, children=NO_CHILDREN):
        """
        Assertion to assert on the structure of this node and it's children.

        This assertion takes a dictionary of properties to assert for this node
        along with a list of assertions to make for it's children.

        Example::

            def test_case(self):
                # Assert the following structure
                #
                # One root_span, with two child_spans, one with a requests.request span
                #
                # |                  root_span                |
                # |       child_span       | |   child_span   |
                # | requests.request |
                self.assert_structure(
                    # Root span with two child_span spans
                    dict(name='root_span'),

                    (
                        # Child span with one child of it's own
                        (
                            dict(name='child_span'),

                            # One requests.request span with no children
                            (
                                dict(name='requests.request'),
                            ),
                        ),

                        # Child span with no children
                        dict(name='child_span'),
                    ),
                )

        :param root: Properties to assert for this root span, these are passed to
            :meth:`tests.utils.span.TestSpan.assert_matches`
        :type root: dict
        :param children: List of child assertions to make, if children is None then do not make any
            assertions about this nodes children. Each list element must be a list with 2 items
            the first is a ``dict`` of property assertions on that child, and the second is a ``list``
            of child assertions to make.
        :type children: list, None
        :raises:
        """
        self.assert_matches(**root)

        # Give them a way to ignore asserting on children
        if children is None:
            return
        elif children is NO_CHILDREN:
            children = ()

        spans = self.spans
        self.assert_span_count(len(children))
        for i, child in enumerate(children):
            if not isinstance(child, (list, tuple)):
                child = (child, NO_CHILDREN)

            root, _children = child
            spans[i].assert_matches(parent_id=self.span_id, trace_id=self.trace_id, _parent=self)
            spans[i].assert_structure(root, _children)

    def pprint(self):
        parts = [super(TestSpanNode, self).pprint()]
        for child in self._children:
            parts.append("-" * 20)
            parts.append(child.pprint())
        return "\r\n".join(parts)


class BaseTracerTestCase(TestSpanContainer, BaseTestCase):
    """
    BaseTracerTestCase is a base test case for when you need access to a dummy tracer and span assertions
    """

    def setUp(self):
        """Before each test case, setup a dummy tracer to use"""
        self.tracer = DummyTracer()

        super(BaseTracerTestCase, self).setUp()

    def tearDown(self):
        """After each test case, reset and remove the dummy tracer"""
        super(BaseTracerTestCase, self).tearDown()

        self.reset()
        delattr(self, "tracer")

    def get_spans(self):
        """Required subclass method for TestSpanContainer"""
        return self.tracer.writer.spans

    def reset(self):
        """Helper to reset the existing list of spans created"""
        self.tracer.writer.pop()

    def trace(self, *args, **kwargs):
        """Wrapper for self.tracer.trace that returns a TestSpan"""
        return TestSpan(self.tracer.trace(*args, **kwargs))

    def start_span(self, *args, **kwargs):
        """Helper for self.tracer.start_span that returns a TestSpan"""
        return TestSpan(self.tracer.start_span(*args, **kwargs))

    def assert_structure(self, root, children=NO_CHILDREN):
        """Helper to call TestSpanNode.assert_structure on the current root span"""
        root_span = self.get_root_span()
        root_span.assert_structure(root, children)

    @contextlib.contextmanager
    def override_global_tracer(self, tracer=None):
        original = ddtrace.tracer
        tracer = tracer or self.tracer
        setattr(ddtrace, "tracer", tracer)
        try:
            yield
        finally:
            setattr(ddtrace, "tracer", original)