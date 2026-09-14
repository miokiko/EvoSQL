"""Local corpus fixtures with a fake vector writer and real corpus retrieval."""
from pathlib import Path
from evoagent.text2sql.vanna_corpus import collect_vanna_corpus
from evoagent.text2sql.vanna_retriever import VannaRetrieverOnly


class FakeVectorWriter:
    def __init__(self, config):
        self.config = config

    def add_ddl(self, value):
        pass

    def add_documentation(self, value):
        pass

    def add_question_sql(self, question, sql):
        pass


def build_test_corpus(root, snapshot, join_catalog=None, question_sql_path=None, business_root=None):
    corpus = collect_vanna_corpus(
        snapshot,
        business_root=business_root or Path(root) / "business",
        join_catalog=join_catalog,
        question_sql_path=question_sql_path,
        excluded_tables=(),
    )
    VannaRetrieverOnly(
        Path(root), corpus["index_version"], enabled=True,
        backend_factory=FakeVectorWriter,
    ).build(corpus["items"], snapshot["snapshot_id"])
    return corpus["index_version"]
