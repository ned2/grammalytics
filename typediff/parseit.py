import sys
import os
import argparse
from collections import Counter, defaultdict

from .delphin import (get_profile_ids, load_hierarchy, TypeNotFoundError,
                      get_profile_results, get_short_label_results,
                      get_text_results, AceError)
from .config import TYPIFIERBIN, ACEBIN
from .gram import get_grammar
from .stats import counts2dist, kl_divergence, js_divergence

"""
A flexible tool for converting and extracting information from
DELPH-IN grammar derivations. Supports reading stored derivations from
DELPH-IN profiles as well as on the fly parsing from text files and
standard input.

For usage, run parseit.py --help

To convert a sequence of profiles to MRG files for fangorn:

# todo: re-run with the PTB tokens removed

# deepbank:
for profile in ~/logon/lingo/erg/tsdb/gold/wsj*; do
    echo $profile
    parseit erg convert ptb $profile > "$(basename $profile).mrg"
done

# redwoods:

# (where redwoods.txt is a text file with profile names of redwoods on
# each line)
for profile in $(cat ../redwoods.txt); do
    echo $profile
    parseit erg convert ptb ~/logon/lingo/erg/tsdb/gold/$profile > "$profile.mrg"
done

"""


# Features that support count mode
COUNT_FEATURES  = ['rules', 'lextypes', 'types']

# Features that don't require reading a grammar's tdl files
NONTDL_FEATURES = ['rules', 'mrs', 'derivation']

# Convert mode features that span multiple lines
MULTILINE_FEATURES = [
    'lextypes', 
    'rules', 
]

# Convert mode features that span only a single line 
SINGLELINE_FEATURES = [
    'derivation',
    'mrs',
    'ptb', 
    'ptb-fangorn', 
    'pprint',
    'latex',
    'short-derivation',
    'generic-ratio', 
    'unknown-ratio', 
    'node-ratio', 
    'input',
    'align',
    'align-errors'
]

# Features that support the convert mode
CONVERT_FEATURES = MULTILINE_FEATURES + SINGLELINE_FEATURES


class UnknownFeatureException(BaseException):
    
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return "Unknown feature: {}".format(self.msg)


OPTSHELP = {
    'gold' : """Specifies that profiles to be queried are gold profiles.""",
    'best' : """Specify the number of readings to use for each item. Default is
1. Has no effect in compare mode.""",
    'cutoff' : """Number of input derivations to stop at.""",
    'parse' : """PATH arguments are treated as text fies which must be parsed.""",
    'align' : """ Ensures output for items are aligned to input items by adding a
placeholder token for items that did not receive a parse.""",
    'failtok' :"""String to print for failed parses when --align is used.  Default
string is 'NULL'.""",
    'backoff' : """Specifies the path to a file containing item features to be used as
a backoff for items with no parse. Currently assumes the same format
as the 'lextypes' feature, but with each item preceeded by its
I-ID.""",
    'le' : """le Use lex entry names for the penultimate node in tree
representations rather than lextypes.""",
    'descendants' : """Restricts types reported on to those that inherit from the type
supplied as an argument. Only relevant when using the 'types'
feature.""",
    'tsql': """An additional tsql constraint, eg 't-active = 1'.""",
    'pspans': """Restrict all results to pertain to derivation subspans
corresponding this this p-id (phenomenon id).""",
}


def argparser():
    ap = argparse.ArgumentParser()
    ap.add_argument("grammar", metavar="GRAMMAR_NAME", help="The alias of a grammar found in config.py")
    ap.add_argument("--gold", action='store_true', help=OPTSHELP['gold'])
    ap.add_argument("--parse", action='store_true', help=OPTSHELP['parse'])
    ap.add_argument("--fragments", action='store_true')
    ap.add_argument("--best", type=int, default=1, help=OPTSHELP['best'])
    ap.add_argument("--cutoff", type=int, help=OPTSHELP['cutoff'])
    ap.add_argument("--tsql", help=OPTSHELP['tsql'])
    ap.add_argument("--pspans", type=int, help=OPTSHELP['pspans'])
    ap.add_argument("--le", action='store_true', help=OPTSHELP['le'])
    ap.add_argument("--debug", action='store_true')
    subparsers = ap.add_subparsers(help='Command help:', dest='command')

    # this assumes paths argument is two sequences of paths separated by '@'
    ap1 = subparsers.add_parser('compare', help='Compare distribuion of attributes across sets of profiles.')
    ap1.add_argument("feature", choices=COUNT_FEATURES, metavar="FEATURE")
    ap1.add_argument("paths", nargs='+', metavar="PATHS")

    ap2 = subparsers.add_parser('convert', help='Extract different kinds of attributes from derivations.')
    ap2.add_argument("--failtok", default='NULL', help=OPTSHELP['failtok'])
    ap2.add_argument("--align", action='store_true', help=OPTSHELP['align'])
    ap2.add_argument("--backoff", help=OPTSHELP['backoff'])
    ap2.add_argument("feature", choices=CONVERT_FEATURES, metavar="FEATURE")
    ap2.add_argument("paths", nargs='*', metavar="PATHS")

    ap3 = subparsers.add_parser('count', help='Count up and sort occurrences of attributes across a collection.')    
    ap3.add_argument("--descendants", help=OPTSHELP['descendants'])
    ap3.add_argument("feature", choices=COUNT_FEATURES, metavar="FEATURE")
    ap3.add_argument("paths", nargs='*', metavar="PATHS")

    ap4 = subparsers.add_parser('draw', help='Draw the derivation with a GUI from NLTK.')
    ap4.add_argument("paths", nargs='*', metavar="PATHS")

    return ap


def reading_features(reading, feature):
    lines = []
    output = None

    if feature == 'lextypes':
        for token in reading.tokens:
            lt = reading.grammar.lex_lookup(token.lex_entry)
            lines.append('{}\t{}'.format(token.string, lt))
    elif feature == 'rules':
        for rule in reading.rules:
            sent_start = '_'.join(t.string for t in reading.tokens[:3])
            lines.append('{}\t{}'.format(sent_start, rule))
    elif feature == 'ptb':
        output = reading.ptb()
    elif feature == 'ptb-fangorn':
        output = "({})".format(reading.ptb())
    elif feature == 'input':
        output = reading.input
    elif feature == 'mrs':
        output = reading.mrs
    elif feature == 'derivation':
        output = reading.derivation       
    elif feature == 'latex':
        output = reading.tree.latex()
    elif feature == 'pprint':
        output = reading.pprint()
    elif feature == 'generic-ratio':
        output = str(sum(reading.generics.values()) / sum(reading.lex_entries.values()))
    elif feature == 'unknown-ratio': 
        output = str(sum(reading.unknowns.values()) / sum(reading.lex_entries.values()))
    elif feature == 'node-ratio':
        output = str(reading.num_nodes / sum(reading.lex_entries.values()))
    elif feature in ('align', 'align-errors'):
        for subtree in reading.subtrees:
            if feature == 'align-errors' and subtree.diff == 0:
                continue
                
            string = 'iid: {}  result-id: {:>2}  diff: {:>2}  match: {}'
            line = string.format(reading.iid, reading.resultid, subtree.diff, subtree.input)

            if DEBUG:
                reading.tree.draw()
                print("DEBUG: " + output)
                
            lines.append(line)
    else:
        raise UnknownFeatureException(feature)

    if len(lines) > 0:
        output = '\n'.join(lines)

    return output


def extract_backoff_items(path):
    results = defaultdict(lambda :'NULL')
    with open(path) as file:
        text = file.read().strip()
        chunks = text.split('\n\n')

        for chunk in chunks:
            lines = chunk.split('\n')
            iid = int(lines[0].lstrip('#'))
            features = "\n".join(lines[1:])
            results[iid] = features

    return results


def convert_trees(results_dict, feature, align, paths, failtok, best, backoff):
    if not align:
        results = sorted(results_dict.items(), key=lambda x:x[0])
        lines = []
        for iid, item in results:
            if feature == "short-derivation":
                lines.extend(item)
                continue

            for reading in item[:best]:
                features = reading_features(reading, feature)
                if features is not None:
                    lines.append(features)
    else:
        if backoff is not None:
            backoffs = extract_backoff_items(backoff)

        lines = []
        for iid in get_profile_ids(*paths):
            try:
                features = reading_features(results_dict[iid][0], feature)
                lines.append(features)
            except IndexError:
                if backoff is not None:
                    lines.append(tagged[iid])
                else:
                    lines.append(failtok)

    if feature in MULTILINE_FEATURES:
        sep = '\n\n'
    else:
        sep = '\n'
    
    return sep.join(lines)


def update_reading_counts(reading, feature, counts, ancestor):
    if feature == 'lextypes':
        counts.update(reading.lextypes)
    elif feature == 'rules':
        counts.update(reading.rules)
    elif feature == 'types':
        if ancestor is None:
            counts.update(reading.types)    
        else:
            hierarchy = load_hierarchy(reading.grammar.types_path)
            try:
                descendant_types = set(t.name for t in hierarchy[ancestor].descendants())
            except TypeNotFoundError as e:
                sys.stderr.write(str(e))
                sys.exit()
            for t in reading.types:
                if t in descendant_types:
                    counts[t] += reading.types[t]
    else:
        raise UnknownFeatureException(feature)


def collection_features(results, feature, ancestor=None):
    counts = Counter()

    for item in results:
        for reading in item:
            update_reading_counts(reading, feature, counts, ancestor)

    return '\n'.join('{}    {}'.format(val,key) for key,val in counts.most_common()) 


def compare_trees(results_dictA, results_dictB, feature):
    resultsA = list(results_dictA.values())
    resultsB = list(results_dictB.values())
    countsA = Counter()
    countsB = Counter()

    for results, counts in ((resultsA, countsA), (resultsB, countsB)):
        for item in results:
            for reading in item:
                update_reading_counts(reading, feature, counts)

    dist1, dist2 = counts2dist(countsA, countsB)
    kl = kl_divergence(dist1, dist2)
    js = js_divergence(dist1, dist2)
    
    lines = [
        "KL divergence of {} = {}".format(feature, kl),
        "JS divergence of {} = {}".format(feature, js),
    ]

    return '\n'.join(lines)


def compare(grammar, arg):
    split = arg.paths.index('@')
    pathsA = arg.paths[:split]
    pathsB = arg.paths[split+1:]
    readingA = get_profile_results(pathsA, best=arg.best, gold=arg.gold, 
                                   cutoff=arg.cutoff, grammar=grammar)
    resultsB = get_profile_results(pathsB, best=arg.best, gold=arg.gold,
                                   cutoff=arg.cutoff, grammar=grammar)

    # TODO: need to turn readingsA and readingsB into dictionaries
    # of {iid:results}. have changed the return value of get_profile_results
    return compare_trees(resultsA, resultsB, arg.feature)


def draw(results_dict):
    import nltk.draw.tree
    from nltk import Tree as NLTKTree
    results = sorted(iter(results_dict.items()), key=lambda x:x[0])
    trees = []
    for i,item in results:
        trees.extend(NLTKTree(r.ptb()) for r in item)
    nltk.draw.tree.draw_trees(*trees)


def get_results(grammar, arg):
    lextypes = not (arg.feature in NONTDL_FEATURES or arg.le)
    cache = (arg.command == 'convert' and arg.feature == 'derivation')

    if arg.command == 'count' and arg.feature == 'types':
        typifier = TYPIFIERBIN
    else:
        typifier = None

    if len(arg.paths) == 0 or arg.parse:
        if len(arg.paths) == 0:
            # read input from standard input
            lines = sys.stdin.readlines()
        else:
            # read lines from from list of input files
            lines = []
            for path in arg.paths:
                with open(path) as f:
                    lines.extend(f.readlines())
        if arg.cutoff is not None:
            lines = lines[:cutoff]
            
        if arg.feature == "short-derivation":
            results = get_short_label_results(
                lines, grammar, best=arg.best, fragments=arg.fragments,
                ace_path=ACEBIN)
        else:
            results = get_text_results(
                lines, grammar, best=arg.best, ace_path=ACEBIN,
                lextypes=lextypes, typifier=typifier, fragments=arg.fragments,
                cache=cache)
    else:
        results = get_profile_results(
            arg.paths, best=arg.best, gold=arg.gold, cutoff=arg.cutoff, 
            grammar=grammar, lextypes=lextypes, typifier=typifier,
            pspans=arg.pspans, condition=arg.tsql, cache=cache)
    return results


def main():
    global DEBUG
    arg = argparser().parse_args()
    DEBUG = arg.debug

    if arg.command == 'convert' and arg.align and arg.best > 1:
        sys.stderr.write("Align option requires that best = 1.\n")
        return 1
    elif arg.command == 'draw':
        arg.feature = None # just hack this rather than working out when defined

    grammar = get_grammar(arg.grammar)
    #if arg.command == 'draw' or arg.feature not in NONTDL_FEATURES:
        #if os.path.basename(arg.paths[0]) in ('vm6', 'vm13', 'vm31', 'vm32'):
        #    # note that this program doesn't support mixing speech and non
        #    # speech profiles in the one invocation. If you need to use speech
        #    # profiles, just run with only speech or only non-speech profiles. 
        #    
        #    grammar.read_tdl(speech=True)
        #else:
        #    grammar.read_tdl(speech=False)
    grammar.read_tdl(speech=False)
        
    try:
        # Do the thing!
        if arg.command == 'compare':
            print(compare(grammar, arg))
        elif arg.command in ('count', 'convert', 'draw'):
            results = get_results(grammar, arg)
            if arg.command == 'count':
                 print(collection_features(list(results.values()), arg.feature, 
                                           arg.descendants))
            elif arg.command == 'convert':
                print(convert_trees(results, arg.feature, arg.align, arg.paths, 
                                    arg.failtok, arg.best, arg.backoff))
            elif arg.command == 'draw':
                draw(results)
    except AceError as e:
        print(e)

    return 0
