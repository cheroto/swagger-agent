"""Tests for ref resolver infrastructure."""

from pathlib import Path

from swagger_agent.infra.resolve import CtagsEntry, build_inheritance_map


class TestBuildInheritanceMap:
    def test_single_parent_single_child(self):
        index = {
            "PaymentMethod": [
                CtagsEntry(name="PaymentMethod", path=Path("pm.cs"), line=1, kind="record"),
            ],
            "CreditCard": [
                CtagsEntry(name="CreditCard", path=Path("cc.cs"), line=1, kind="record",
                           inherits="PaymentMethod"),
            ],
        }
        imap = build_inheritance_map(index)
        assert "PaymentMethod" in imap
        assert len(imap["PaymentMethod"]) == 1
        assert imap["PaymentMethod"][0].name == "CreditCard"

    def test_multiple_children(self):
        index = {
            "Animal": [
                CtagsEntry(name="Animal", path=Path("a.py"), line=1, kind="class"),
            ],
            "Dog": [
                CtagsEntry(name="Dog", path=Path("d.py"), line=1, kind="class",
                           inherits="Animal"),
            ],
            "Cat": [
                CtagsEntry(name="Cat", path=Path("c.py"), line=1, kind="class",
                           inherits="Animal"),
            ],
        }
        imap = build_inheritance_map(index)
        children = {e.name for e in imap["Animal"]}
        assert children == {"Dog", "Cat"}

    def test_multiple_parents_comma_separated(self):
        """C#/Java: class Foo : Base, IInterface → inherits="Base,IInterface"."""
        index = {
            "Foo": [
                CtagsEntry(name="Foo", path=Path("f.cs"), line=1, kind="class",
                           inherits="Base,ISerializable"),
            ],
        }
        imap = build_inheritance_map(index)
        assert "Base" in imap
        assert "ISerializable" in imap
        assert imap["Base"][0].name == "Foo"
        assert imap["ISerializable"][0].name == "Foo"

    def test_no_inherits(self):
        index = {
            "User": [
                CtagsEntry(name="User", path=Path("u.py"), line=1, kind="class"),
            ],
        }
        imap = build_inheritance_map(index)
        assert imap == {}

    def test_empty_index(self):
        assert build_inheritance_map({}) == {}
