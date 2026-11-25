#!/usr/bin/env bash
# A repository `test` (admin / sekretsekret) with dummy schema.org content: COUNT persons
# (records rec:person:<n>), COUNT/200 organizations, COUNT/1000 series and collections and
# COUNT/2 works (a fourth of them articles in the issue of a volume of a journal; every
# second series is a journal), that point at each other by their resource uris (ark:/99999/ir/<type>/<n>).
# Organizations and collections have a parent now and then (a link, as every other).
# Needs `gracia serve-db` running and the venv active; run it where config.ini is.
set -euo pipefail

COUNT=${1:-100000}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

(umask 077; echo sekretsekret > "$TMP/password")
gracia repo create test --admin admin --admin-password-file "$TMP/password"
gracia -r test types add person organization series collection work

awk -v n="$COUNT" '
function pick(list, size) { return list[int(rand() * size) + 1] }
function day(year) { return sprintf("%04d-%02d-%02d", year, 1 + int(rand() * 12), 1 + int(rand() * 28)) }
function ark(type, number) { return "<ark:/99999/ir/" type "/" number ">" }
function node(type, number) {
  printf "<rec:%s:%d> a gracia:Record ;\n  gracia:resource_uri %s ;\n", type, number, ark(type, number)
}
BEGIN {
  split("Ada Alan Grace Linus Margaret Dennis Barbara Ken Edsger Donald Frances Tim Radia Guido Hedy Bjarne Sophie Brian Katherine Niklaus", g, " ")
  split("Lovelace Turing Hopper Torvalds Hamilton Ritchie Liskov Thompson Dijkstra Knuth Allen Berners-Lee Perlman van-Rossum Lamarr Stroustrup Wilson Kernighan Johnson Wirth", f, " ")
  split("University Institute Library Academy Press Foundation", kind, " ")
  split("Amsterdam Delft Leiden Utrecht Groningen Rotterdam Eindhoven Maastricht Tilburg Wageningen", city, " ")
  split("Algorithms Compilers Databases Networks Graphics Security Languages Logic Systems Robotics", topic, " ")
  split("Early Modern Applied Formal Practical Concise Open Distributed Annotated Complete", adjective, " ")
  split("Computation Notation Reasoning Memory Search Types Proofs Machines Patterns Data", noun, " ")
  split("Book ScholarlyArticle Thesis Report Article", work_type, " ")
  split("en nl de fr", language, " ")
  print "@prefix gracia: <https://gracia.pub/ns/> ."
  print "@prefix schema: <https://schema.org/> ."
  print "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> ."
  srand(42)
  orgs = int(n / 200); if (orgs < 1) orgs = 1
  series = int(n / 1000); if (series < 2) series = 2  # the even ones are journals
  collections = int(n / 1000); if (collections < 1) collections = 1
  works = int(n / 2); if (works < 1) works = 1

  for (i = 1; i <= n; i++) {
    a = pick(g, 20); b = pick(f, 20)
    node("person", i)
    printf "  gracia:resource [ a schema:Person ; schema:name \"%s %s\" ; schema:givenName \"%s\" ; schema:familyName \"%s\" ; schema:alternateName \"%s%d\"", a, b, a, b, tolower(substr(a, 1, 1) b), i
    if (i % 5 == 0) printf " ; schema:email \"%s.%s%d@example.org\"", tolower(a), tolower(b), i
    print " ] ."
  }

  for (i = 1; i <= orgs; i++) {
    name = pick(kind, 6) " of " pick(city, 10)
    node("organization", i)
    printf "  gracia:resource [ a schema:Organization ; schema:name \"%s\" ; schema:legalName \"Stichting %s %d\" ; schema:url <https://organization%d.example.org/> ; schema:description \"A dummy organization.\"", name, name, i, i
    if (i > 4 && i % 4 == 0) printf " ; schema:parentOrganization %s", ark("organization", int(rand() * (i - 1)) + 1)
    print " ] ."
  }

  for (i = 1; i <= series; i++) {
    node("series", i)
    if (i % 2 == 0)
      printf "  gracia:resource [ a schema:Periodical ; schema:name \"Journal of %s %d\"", pick(topic, 10), i
    else
      printf "  gracia:resource [ a schema:BookSeries ; schema:name \"Lecture Notes in %s %d\"", pick(topic, 10), i
    issn = sprintf("%04d-%04d", 1000 + i % 9000, int(rand() * 10000))
    printf " ; schema:identifier [ a schema:PropertyValue ; schema:propertyID \"ISSN\" ; schema:value \"%s\" ; schema:url <urn:issn:%s> ]", issn, issn
    printf " ; schema:publisher %s ; schema:inLanguage \"en\" ] .\n", ark("organization", int(rand() * orgs) + 1)
  }

  for (i = 1; i <= collections; i++) {
    node("collection", i)
    printf "  gracia:resource [ a schema:Collection ; schema:name \"%s Theses %d\" ; schema:description \"A dummy collection.\" ; schema:collectionSize %d ; schema:publisher %s", pick(city, 10), i, int(rand() * 500), ark("organization", int(rand() * orgs) + 1)
    if (i > 4 && i % 4 == 0) printf " ; schema:isPartOf %s", ark("collection", int(rand() * (i - 1)) + 1)
    print " ] ."
  }

  for (i = 1; i <= works; i++) {
    node("work", i)
    if (i % 4 == 0) {  # an article: of a volume and an issue of a journal
      journal = 2 * (int(rand() * int(series / 2)) + 1)
      year = 1950 + int(rand() * 76); issued = day(year); first = 1 + int(rand() * 300)
      printf "  gracia:resource [ a schema:ScholarlyArticle ; schema:name \"%s %s of %s %d\" ; schema:author ( %s )", pick(adjective, 10), pick(noun, 10), pick(topic, 10), i, ark("person", int(rand() * n) + 1)
      printf " ; schema:pageStart \"%d\" ; schema:pageEnd \"%d\" ; schema:datePublished \"%s\"^^xsd:date ; schema:inLanguage \"en\"", first, first + 5 + int(rand() * 20), issued
      printf " ; schema:volumeNumber \"%d\" ; schema:issueNumber \"%d\" ; schema:isPartOf %s ] .\n", year - 1949, 1 + int(rand() * 12), ark("series", journal)
      continue
    }
    printf "  gracia:resource [ a schema:%s ; schema:name \"%s %s of %s %d\" ; schema:author ( %s", pick(work_type, 5), pick(adjective, 10), pick(noun, 10), pick(topic, 10), i, ark("person", int(rand() * n) + 1)
    if (i % 3 == 0) printf " %s", ark("person", int(rand() * n) + 1)
    printf " )"
    # now and then a contributor in a role: a person who advised it, an organization that paid
    if (i % 5 == 0) printf " ; schema:contributor [ a schema:Role ; schema:roleName <http://example.org/vocab/role/Expert> ; schema:contributor %s ]", ark("person", int(rand() * n) + 1)
    if (i % 7 == 0) printf " ; schema:contributor [ a schema:Role ; schema:roleName <http://example.org/vocab/role/Funder> ; schema:contributor %s ]", ark("organization", int(rand() * orgs) + 1)
    printf " ; schema:publisher %s ; schema:isPartOf %s ; schema:datePublished \"%s\"^^xsd:date ; schema:inLanguage \"%s\" ; schema:keywords \"%s, %s\"", ark("organization", int(rand() * orgs) + 1), (i % 2 ? ark("series", int(rand() * series) + 1) : ark("collection", int(rand() * collections) + 1)), day(1950 + int(rand() * 76)), pick(language, 4), tolower(pick(topic, 10)), tolower(pick(noun, 10))
    print " ] ."
  }
}' > "$TMP/content.ttl"

time gracia -r test import "$TMP/content.ttl"
