#!/usr/bin/env bash
# A repository `test` (admin / sekretsekret) with 100,000 dummy foaf persons (records rec:person:<n>).
# Needs `gracia serve-db` running and the venv active; run it where config.ini is.
set -euo pipefail

COUNT=${1:-100000}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

(umask 077; echo sekretsekret > "$TMP/password")
gracia repo create test --admin admin --admin-password-file "$TMP/password"
gracia -r test types add person

awk -v n="$COUNT" '
BEGIN {
  split("Ada Alan Grace Linus Margaret Dennis Barbara Ken Edsger Donald Frances Tim Radia Guido Hedy Bjarne Sophie Brian Katherine Niklaus", g, " ")
  split("Lovelace Turing Hopper Torvalds Hamilton Ritchie Liskov Thompson Dijkstra Knuth Allen Berners-Lee Perlman van-Rossum Lamarr Stroustrup Wilson Kernighan Johnson Wirth", f, " ")
  print "@prefix gracia: <https://gracia.pub/ns/> ."
  print "@prefix foaf: <http://xmlns.com/foaf/0.1/> ."
  srand(42)
  for (i = 1; i <= n; i++) {
    a = g[int(rand() * 20) + 1]; b = f[int(rand() * 20) + 1]
    printf "<rec:person:%d> a gracia:Record ;\n  gracia:resource_uri <ark:/99999/ir/person/%d> ;\n", i, i
    printf "  gracia:resource [ a foaf:Person ; foaf:name \"%s %s\" ; foaf:givenName \"%s\" ; foaf:familyName \"%s\" ; foaf:nick \"%s%d\" ] .\n", a, b, a, b, tolower(substr(a, 1, 1) b), i
  }
}' > "$TMP/persons.ttl"

time gracia -r test import "$TMP/persons.ttl"
