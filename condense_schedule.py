import csv
import json
import re
import sys
from pathlib import Path

MEETING = re.compile(r'(?<![A-Za-z])([MTWRFSU]{1,7})\s+(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)', re.I)
DAYS = {'M':'Monday','T':'Tuesday','W':'Wednesday','R':'Thursday','F':'Friday','S':'Saturday','U':'Sunday'}
COURSE = re.compile(r'^[A-Z]{2,4}[A-Z0-9]{5}$')

def meetings_from(text):
    result=[]
    for match in MEETING.finditer(text or ''):
        days, start, end = match.groups()
        entry={'days':list(dict.fromkeys(DAYS[d] for d in days.upper())),
               'start_time':re.sub(r'\s+',' ',start.upper()).lstrip('0'),
               'end_time':re.sub(r'\s+',' ',end.upper()).lstrip('0')}
        if entry not in result: result.append(entry)
    return result

def convert(src, dst, include_titles=False, skip_cancelled=True):
    sections=[]; skipped=[]; warnings=[]
    with open(src, newline='', encoding='utf-8-sig') as f:
        for line,row in enumerate(csv.DictReader(f),start=2):
            crn=(row['CRN'] or '').strip()
            if not crn: continue
            if skip_cancelled and row['Cancelled'].strip().lower()=='true': continue
            course_id=(row['Subj']+row['Course#']).replace(' ','').upper()
            if not COURSE.fullmatch(course_id):
                skipped.append((line,crn,course_id));continue
            try:
                hours=float(row['Hours']); total=int(row['Max']); taken=int(row['Enrolled'])
            except ValueError:
                skipped.append((line,crn,'invalid numeric value'));continue
            campus=row['Campus'].strip()
            if not campus:
                skipped.append((line,crn,'missing campus'));continue
            session=row['Session'].strip().lower()
            modality='hybrid' if session=='hybrid' else ('online' if session=='online' or campus.lower() in ('online','remote') else 'in-person')
            raw_meeting=row['Meeting Time'].strip()
            meetings=meetings_from(raw_meeting)
            if raw_meeting and raw_meeting.upper()!='N/A' and not meetings:
                warnings.append((line,crn,'unparsed meeting time'))
            section={'crn':crn,'course_id':course_id,'section':row['Sect'].strip(),
                     'credit_hours':hours,'seats_total':total,'seats_taken':taken,
                     'campus':campus,'modality':modality,'meetings':meetings}
            if include_titles and row['Title'].strip(): section['title']=row['Title'].strip()
            sections.append(section)
    schedule={'term':'Fall 2026','sections':sections}
    Path(dst).write_text(json.dumps(schedule,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'Exported {len(sections)} sections to {dst}')
    print(f'Skipped {len(skipped)} rows with invalid course IDs or fields; {len(warnings)} meeting-time parsing warnings.')
    for item in (skipped[:12]+warnings[:12]):print('CHECK',item)
    print(f'File size: {Path(dst).stat().st_size:,} bytes')
    return schedule

if __name__=='__main__':
    src=Path(sys.argv[1]) if len(sys.argv)>1 else Path('section_tally_fall2026.csv')
    out=Path(sys.argv[2]) if len(sys.argv)>2 else src.with_name('reduced_schedule.json')
    convert(src,out,include_titles='--titles' in sys.argv)
