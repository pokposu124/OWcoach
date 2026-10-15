#!/usr/bin/env python3
"""Read-only lookup: hero, ability, text search, and review queue."""
import argparse
import json
from pathlib import Path
import sqlite3


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--db', default=str(Path(__file__).with_name('overwatch.sqlite')))
    s=p.add_subparsers(dest='command',required=True)
    h=s.add_parser('hero');h.add_argument('hero')
    a=s.add_parser('ability');a.add_argument('hero');a.add_argument('ability')
    f=s.add_parser('search');f.add_argument('terms');f.add_argument('--limit',type=int,default=8)
    s.add_parser('review')
    args=p.parse_args()
    db=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    if args.command in ('hero','ability'):
        row=db.execute('SELECT * FROM heroes WHERE hero_id=? OR name_en=? COLLATE NOCASE OR name_ko=?',
                       (args.hero,args.hero,args.hero)).fetchone()
        if not row:raise SystemExit('Hero not found')
        if args.command=='hero':
            result=json.loads(row['data_json'])
        else:
            matches=db.execute('SELECT data_json FROM abilities WHERE hero_id=? AND (name_en=? COLLATE NOCASE OR ability_id=?)',
                               (row['hero_id'],args.ability,row['hero_id']+':'+args.ability)).fetchall()
            if not matches:raise SystemExit('Ability not found')
            result=[json.loads(r[0]) for r in matches]
    elif args.command=='search':
        # Quote user words as FTS literals; do not interpret FTS operators by default.
        query=' AND '.join('"'+w.replace('"','""')+'"' for w in args.terms.split())
        rows=db.execute('SELECT ability_id,hero_id,name_en,description FROM ability_search WHERE ability_search MATCH ? ORDER BY rank LIMIT ?',
                        (query,max(1,min(args.limit,100)))).fetchall()
        result=[dict(r) for r in rows]
    else:
        result={'official_unresolved':[dict(r) for r in db.execute('SELECT * FROM review_queue ORDER BY hero_id,field_path')],
                'manual_reviews':[dict(r) for r in db.execute('SELECT * FROM manual_reviews ORDER BY hero_id')]}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
