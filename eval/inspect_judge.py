import json
import glob
from eval.scoring import cohens_kappa

res = json.load(open('results/multicat_none.dev.json', encoding='utf-8'))
print(f"Loaded {len(res['per_case'])} cases from results/multicat_none.dev.json")

# Gather all (finding, fix_patch) pairs evaluated
judge_files = glob.glob('cache/judge/*.json')
print(f"Total cached judge calls: {len(judge_files)}")

# Read judge files
judge_verdicts = []
for jf in judge_files:
    try:
        d = json.loads(open(jf, encoding='utf-8').read())
        t = json.loads(d['text'])
        judge_verdicts.append(t)
    except Exception as e:
        pass

print(f"Valid judge verdicts extracted: {len(judge_verdicts)}")
true_count = sum(bool(v.get('matches_root_cause')) for v in judge_verdicts)
false_count = len(judge_verdicts) - true_count
print(f"Judge judgments: {true_count} True, {false_count} False")
for i, v in enumerate(judge_verdicts[:10]):
    print(f"  [{i}] match={v.get('matches_root_cause')}: {v.get('reasoning')[:80]}")
