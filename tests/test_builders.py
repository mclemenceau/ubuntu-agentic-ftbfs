"""Builder pool: slot accounting, host choice, arch support, config."""

import threading
import time

import pytest

from ftbfs.builder.local import LocalBuilder
from ftbfs.builder.pool import BuilderPool, NoBuilder, make_pool


def pool(**slots) -> BuilderPool:
    return BuilderPool([LocalBuilder(n, s) for n, s in slots.items()])


def test_picks_the_builder_with_most_free_slots():
    p = pool(a=1, b=3)
    with p.slot("amd64") as first, p.slot("amd64") as second, \
            p.slot("amd64") as third:
        # b: 3 free vs 1; then 2 vs 1; then a and b tie at 1 free.
        assert [first.name, second.name] == ["b", "b"]
        assert third.name in ("a", "b")
        assert sum(p.busy().values()) == 3
    assert p.busy() == {"a": 0, "b": 0}


def test_waits_for_a_free_slot():
    p = pool(a=1)
    order = []

    def second():
        with p.slot("amd64"):
            order.append("second")

    with p.slot("amd64"):
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.2)
        order.append("first done")
    t.join(5)
    assert order == ["first done", "second"]


def test_slot_is_released_when_the_build_raises():
    p = pool(a=1)
    with pytest.raises(RuntimeError), p.slot("amd64"):
        raise RuntimeError("boom")
    assert p.busy() == {"a": 0}


def test_arch_support():
    p = BuilderPool([LocalBuilder("a", 1),
                     LocalBuilder("b", 1, arches=("amd64", "i386"))])
    assert p.supports("i386") and not p.supports("arm64")
    with p.slot("i386") as b:
        assert b.name == "b"
    with pytest.raises(NoBuilder), p.slot("arm64"):
        pass


def test_down_builders_are_skipped_until_their_time_is_up():
    p = pool(a=3, b=1)
    p.mark_down("a", "unreachable", for_s=0.3)
    assert p.down() == {"a": "unreachable"}
    with p.slot("amd64") as b:
        assert b.name == "b"
    time.sleep(0.4)
    assert p.down() == {}
    with p.slot("amd64") as b:
        assert b.name == "a"


def test_all_builders_down_fails_fast():
    p = pool(a=1, b=1)
    p.mark_down("a", "no image")
    p.mark_down("b", "unreachable")
    with pytest.raises(NoBuilder, match="a: no image; b: unreachable"), \
            p.slot("amd64"):
        pass


def test_waiting_builds_fail_when_the_last_host_goes_down():
    p = pool(a=1)
    errors = []

    def waiter():
        try:
            with p.slot("amd64"):
                pass
        except NoBuilder as e:
            errors.append(e)

    with p.slot("amd64"):
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        p.mark_down("a", "gone")
    t.join(5)
    assert len(errors) == 1


def test_make_pool_default_is_one_local_builder():
    p = make_pool({}, 4)
    assert [(b.name, b.slots) for b in p.builders] == [("local", 4)]
    assert p.slots == 4


def test_make_pool_from_config():
    p = make_pool({"local": {"slots": 3, "parallel": 6},
                   "other": {"slots": 2, "arches": ["amd64", "i386"]}}, 4)
    assert p.slots == 5
    local, other = p.builders
    assert local.parallel == 6 and other.parallel is None
    assert other.arches == ("amd64", "i386")


def test_make_pool_lxd():
    p = make_pool({"host": {"kind": "lxd", "remote": "buildhost",
                            "slots": 2, "image": "other"}}, 4)
    (b,) = p.builders
    assert (b.remote, b.image, b.workers) == (
        "buildhost", "other", ["ftbfs-host-1", "ftbfs-host-2"])


@pytest.mark.parametrize("conf, error", [
    ({"x": {"kind": "nope"}}, "unknown kind"),
    ({"x": {"slots": 1, "typo": 1}}, "unknown keys"),
])
def test_make_pool_rejects_bad_config(conf, error):
    with pytest.raises(ValueError, match=error):
        make_pool(conf, 4)
