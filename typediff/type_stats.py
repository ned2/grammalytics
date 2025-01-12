import sys
import os
import argparse
import pickle
import json

from subprocess import Popen, PIPE
from collections import Counter, defaultdict

from .delphin import (TypeStats, tsdb_query, TsdbError, AceError, AceError,
                      load_hierarchy, JSONEncoder)
from .config import TYPIFIERBIN
from .gram import get_grammar


"""This script is for working with type statistics from DELPH-IN
treebanks. Funcitonality includes collecting treebank statisitcs,
saving as pickle files for storage, loading from existing pickle file
and exporting to text and json formats.

"""

# TODO: change index mode to create json. remove pickling entirely
# this then means output mode should become text mode. which I don't
# really need, but hey why not keep it.


BLACKLIST = set()
ERG_SPEECH_PROFILES = set(['vm6', 'vm13', 'vm13', 'vm31', 'vm32', 'ecpa',
                           'ecoc', 'ecos', 'ecpr'])


class Usage(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return "Argument parsing error: {}".format(self.msg)


def argparser():
    argparser = argparse.ArgumentParser()
    subparsers = argparser.add_subparsers(dest="command")

    iparser = subparsers.add_parser('index', help='index the types used in a treebank')
    iparser.add_argument("profile", metavar="PROFILE")
    iparser.add_argument("grammar", metavar="GRAMMAR_ALIAS")
    iparser.add_argument("treebank", metavar="NAME_OF_TREEBANK")
    iparser.add_argument("--multi", action='store_true')

    oparser = subparsers.add_parser('output', help='produce output based on a previously generated index')
    oparser.add_argument("type", choices=('json', 'txt'), metavar="OUTPUT_TYPE")
    oparser.add_argument("path", metavar="PATH_TO_PICKLE_FILE")
    return argparser


def index(profiles, treebank, in_grammar):
    stats_dict = defaultdict(TypeStats)
    trees = 0
    failures = []


    for path in profiles:
        grammar = in_grammar
        items_seen = set()
        print("processing {}".format(path))
        profile = os.path.basename(path) 

        if profile in ERG_SPEECH_PROFILES:
            # let's just  ignore them for now
            continue
            alias = grammar.alias+'-speech'
            grammar = get_grammar(alias)

        try:
            # for treebanked profiles:
            out = tsdb_query('select i-id derivation where t-active > 0', path)
            # for non-treebanked profiles
            # out = tsdb_query('select i-id derivation where readings > 0', path)
        except TsdbError as e:
            with open('tsdb_errors.txt', 'a', encoding='utf8') as f:
                f.write(str(e) + '\n')

            sys.stderr.write(str(e)+'\n')
            continue

        if out == '':
            continue

        results = out.strip().split('\n')
        for result in results:
            iid, derivation = result.split(' | ')

            if iid in items_seen or iid in BLACKLIST:
                continue
                
            try:
                assert grammar is not None
                counts = get_types(derivation, grammar)
                for name, count in counts.items():
                    stats_dict[name].update(count)
            except AceError as e:
                e.other_data.append(iid)
                e.other_data.append(path)
                failures.append(e)
                with open('ace_errors.txt', 'a', encoding='utf8') as f:
                    f.write(str(e) + '\n')
                sys.stderr.write(str(e) + '\n')
            else:
                items_seen.add(iid)
                trees += 1
                print(trees, iid)

    print("Processed {} trees".format(trees))

    treebank_str = treebank.replace(' ', '_')
    filename = '{}--{}--{}.pickle'.format(grammar.alias, treebank_str, trees)

    with open(filename, 'wb') as f:
        pickle.dump(stats_dict, f)

    num_failures = len(failures)
    if num_failures > 0: 
        print("Failed to reconstruct {} trees".format(num_failures))
        print("See type-stats-errors.txt for details.")

        with open('type-stats-errors.txt', 'w', encoding='utf8') as f:
            errors_str = '\n'.join(str(e) for e in failures) + '\n\n'
            f.write(errors_str)


def get_types(derivation_string, grammar):
    env = dict(os.environ)
    env['LC_ALL'] = 'en_US.UTF-8'
    args = [TYPIFIERBIN, grammar.dat_path]
    process= Popen(args, stdout=PIPE, stdin=PIPE, stderr=PIPE, env=env)
    out, err = process.communicate(input=derivation_string.encode('utf8'))
    out = out.decode('utf8')
    err = err.decode('utf8')

    if err != '':
        raise AceError('typifier', err)
    
    types, tree = out.split('\n\n')
    err = err
    types = (t for t in types.split() if not t.startswith('"') or
             (t.endswith('_rel"') and not t.endswith('unknown_rel"')))
    return Counter(types)


def output(pickle_path, output_type):
    with open(pickle_path, 'rb') as f:
        type_stats = pickle.load(f)

    x = os.path.splitext(os.path.basename(pickle_path))[0].split('--')
    grammar_name = x[0]
    treebank = x[1]
    trees = x[2]
    metadata = {'grammar' : grammar_name, 'treebank' : treebank, 'trees' : trees}

    grammar = get_grammar(grammar_name)
    hierarchy = load_hierarchy(grammar.types_path)
    signs = [x.name for x in hierarchy['sign'].descendants() 
             if not x.name.startswith('glb')]
 
    if output_type == 'txt':
        lex_entries = filter(lambda x:x.endswith('_le'), signs)
        rules = filter(lambda x:not x.endswith('_le'), signs)
        unknowns = filter(lambda x:x.endswith('unknown_rel"'), type_stats.keys()) 
        all_types = [x for x in hierarchy.types if not x.startswith('glb')]
        txt_output(lex_entries, type_stats, metadata, 'lex')
        txt_output(rules, type_stats, metadata, 'rule')
        txt_output(type_stats.keys(), type_stats, metadata, 'everything')
        txt_output(unknowns, type_stats, metadata, 'unknowns')
    elif output_type == 'json':
        json_output(type_stats, metadata)

    
def json_output(type_stats, metadata):
    filename = "{grammar}--{treebank}--{trees}.json".format(**metadata)

    with open(filename, 'w') as f:
        f.write(json.dumps(type_stats, cls=JSONEncoder))


def txt_output(types, type_stats, metadata, kind):
    lines = []

    key_func = lambda x:0 if x not in type_stats else type_stats[x].items
    types.sort(reverse=True, key=key_func)
                
    for name in types:
        try:
            t = type_stats[name]
            items = t.items
            counts = t.counts
        except KeyError:
            items = 0
            counts = 0
            
        if kind == 'unknowns':
            line = "{:50}{:10}{:15}".format(name, items, counts)
        else:
            line = "{:50}{:10.2f}{:15}".format(name, items*100/int(metadata['trees']), counts)

        lines.append(line)

    filename = "{grammar}--{treebank}--{trees}--{0}.txt".format(kind, **metadata)
    with open(filename, 'w') as f:
        f.write('\n'.join(lines))
                    

def main():
    arg = argparser().parse_args()
    profiles = []

    if arg.command == 'index':
        if arg.multi:
            for name in os.listdir(arg.profile):
                path = os.path.join(arg.profile, name)
                if not os.path.isdir(path) or name.startswith('.'):
                    continue
                profiles.append(path)
        else:
            virtual_path = os.path.join(arg.profile, 'virtual')
            if os.path.exists(virtual_path):
                with open(virtual_path) as file:
                    profiles = [os.path.join(arg.profile, '..', p.strip('"\n'))
                                for p in file]
            else:
                profiles.append(arg.profile)

        grammar = get_grammar(arg.grammar)
        index(profiles, arg.treebank, grammar)

    elif arg.command == 'output':
        output(arg.path, arg.type)
        

if __name__ == "__main__":
    main()
