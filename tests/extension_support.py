import pyoxigraph as ox

from gracia import init, uri

PLACEHOLDER = ox.NamedNode("urn:gracia:content")


def put_extension_state(
    application, name="demo", settings=None, enabled=None, turtle=None, repo="test_repo"
):
    with application.new_store(repo) as store:
        repo_uri = uri.repository_uri()
        data = store.get(repo_uri)
        content = data.value(ox.NamedNode(repo_uri), ox.NamedNode(uri.RESOURCE))
        text = turtle or init.extension_turtle(
            f"<{PLACEHOLDER.value}>", name, settings, enabled
        )
        for quad in ox.parse(text.encode(), format=ox.RdfFormat.TURTLE):
            owner = content if quad.subject == PLACEHOLDER else quad.subject
            data.add(ox.Quad(owner, quad.predicate, quad.object, ox.DefaultGraph()))
        store.put_multi(data)
