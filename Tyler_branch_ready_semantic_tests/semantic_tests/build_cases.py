import json
from pathlib import Path
from copy import deepcopy
from collections import Counter

OUT=Path(__file__).resolve().parent
cases=[]

def section(crn,course,number,title,days,start,end,*,campus='Glassboro',modality='in-person', credits=3):
    meetings=[] if not days else [{'days':days,'start_time':start,'end_time':end,'type':'Lecture'}]
    return dict(crn=crn,course_id=course,section=number,title=title,credit_hours=credits,
                seats_total=30,seats_taken=5,campus=campus,modality=modality,meetings=meetings)

def base(student='S001', sections=None, *, completed=None, in_progress=None, requirements=None):
    return {
      'student':{'student_id':student,'degree':'Bachelor of Science','major':'Computer Science',
                 'program_id':'O701','catalog_year':'Fall 2026'},
      'transcript':{'completed_courses':completed or [],'transfer_courses':[],
                    'courses_in_progress':in_progress or []},
      'program_requirements': requirements or [],
      'schedule':{'term':'Fall 2026','sections':deepcopy(sections or [])},
      'prompt':''
    }

def add(pair,idx,case_input,prompt,expected,wrong,dimension,reason,variation):
    inp=deepcopy(case_input)
    inp['prompt']=prompt
    cases.append({
      'case_id':f'SEM-{len(cases)+1:03d}',
      'scenario_id':pair,
      'variation':variation,
      'semantic_dimension':dimension,
      'input':inp,
      'ground_truth':{'acceptable_crns':[expected],
                      'wrong_but_consistent_crns':[wrong],
                      'reason':reason},
      'review_status':'candidate_needs_human_review'
    })

# 1 Campus - prompt is the only place that expresses student's desired location
ss=[section('31001','CS05000','01','Software Engineering',['Monday','Wednesday'],'10:00 AM','11:15 AM',campus='Glassboro'),
    section('31002','CS05000','02','Software Engineering',['Tuesday','Thursday'],'10:00 AM','11:15 AM',campus='Camden')]
b=base(sections=ss)
add('campus',1,b,'Register me for CS05000. I can attend only at the Glassboro campus.','31001','31002','campus_preference','Camden is open but contradicts the explicit campus restriction.','Glassboro required')
add('campus',2,b,'Register me for CS05000. I can attend only at the Camden campus.','31002','31001','campus_preference','Glassboro is open but contradicts the explicit campus restriction.','Camden required')

# 2 Modality
ss=[section('32001','CS06000','01','Database Systems',['Monday','Wednesday'],'11:00 AM','12:15 PM',modality='in-person'),
    section('32002','CS06000','02','Database Systems',[],None,None,modality='online')]
b=base(sections=ss)
add('modality',1,b,'Enroll me in CS06000. I need an online section.','32002','32001','modality_preference','The in-person section is open but fails the requested modality.','online required')
add('modality',2,b,'Enroll me in CS06000. I need an in-person section.','32001','32002','modality_preference','The online section is open but fails the requested modality.','in-person required')

# 3 Weekday
ss=[section('33001','CS07000','01','Systems Topics',['Monday','Wednesday'],'1:00 PM','2:15 PM'),
    section('33002','CS07000','02','Systems Topics',['Tuesday','Thursday'],'1:00 PM','2:15 PM')]
b=base(sections=ss)
add('weekday',1,b,'Enroll me in CS07000 on Mondays and Wednesdays.','33001','33002','meeting_days','Both options have seats, but TR does not match the requested days.','MW requested')
add('weekday',2,b,'Enroll me in CS07000 on Tuesdays and Thursdays.','33002','33001','meeting_days','Both options have seats, but MW does not match the requested days.','TR requested')

# 4 Time of day
ss=[section('34001','CS08000','01','AI Topics',['Monday','Wednesday'],'9:00 AM','10:15 AM'),
    section('34002','CS08000','02','AI Topics',['Tuesday','Thursday'],'3:00 PM','4:15 PM')]
b=base(sections=ss)
add('time_of_day',1,b,'Sign me up for the morning offering of CS08000.','34001','34002','time_preference','3:00 PM is not morning.','morning')
add('time_of_day',2,b,'Sign me up for the afternoon offering of CS08000.','34002','34001','time_preference','9:00 AM is not afternoon.','afternoon')

# 5 Negative constraint
ss=[section('35001','CS09000','01','Software Testing',['Monday','Wednesday'],'10:00 AM','11:15 AM'),
    section('35002','CS09000','02','Software Testing',['Tuesday','Thursday'],'10:00 AM','11:15 AM')]
b=base(sections=ss)
add('negation',1,b,'Enroll me in CS09000, but do not use CRN 35001.','35002','35001','negation','Choosing explicitly prohibited CRN 35001 is a semantic mistake.','exclude 35001')
add('negation',2,b,'Enroll me in CS09000, but do not use CRN 35002.','35001','35002','negation','Choosing explicitly prohibited CRN 35002 is a semantic mistake.','exclude 35002')

# 6 Previous grade and minimum requirement
ss=[section('36001','CS04113','01','Intro to Programming',['Monday','Wednesday'],'1:00 PM','2:15 PM',credits=4),
    section('36002','CS05000','02','Software Engineering',['Tuesday','Thursday'],'2:00 PM','3:15 PM')]
reqs=[{'req_id':'intro_programming','category':'Major Foundation','rule':'all_of',
       'course_options':['CS04113'],'title':'Introduction to Programming',
       'credit_hours':4,'minimum_grade':'C-'}]
def completed(grade):return [{'term':'Spring 2026','course_id':'CS04113','grade':grade,'credit_hours':4}]
b_low=base(sections=ss,completed=completed('D'),requirements=reqs)
b_high=base(sections=ss,completed=completed('B'),requirements=reqs)
prompt='I need a C- or better in CS04113. If I have not met that grade, enroll me in CS04113 again; otherwise enroll me in CS05000.'
add('grade_threshold',1,b_low,prompt,'36001','36002','transcript_grade_requirement','D is below C-, so the repeated CS04113 section is intended.','D (insufficient)')
add('grade_threshold',2,b_high,prompt,'36002','36001','transcript_grade_requirement','B meets C-, so the requested fallback is CS05000.','B (sufficient)')

# 7 Degree requirement / course credit eligibility
ss=[section('37001','CS02435','01','Computer Science Elective',['Monday','Wednesday'],'2:00 PM','3:15 PM'),
    section('37002','MATH03000','01','Applied Mathematics',['Tuesday','Thursday'],'2:00 PM','3:15 PM')]
requirements=[{'req_id':'restricted_elective','category':'CS Restricted Electives','rule':'credits_from',
              'course_options':['CS02435','CS02480'],'title':'Restricted Elective Credits',
              'credit_hours':6,'minimum_grade':'C-'}]
b=base(sections=ss,requirements=requirements)
add('requirement_goal',1,b,'I want a class that counts toward the listed CS Restricted Electives requirement. Enroll me in an eligible course.','37001','37002','degree_progress','MATH03000 is available but is not among the elective course_options.','elective progress')
add('requirement_goal',2,b,'I specifically need the Applied Mathematics course, not a CS restricted elective. Enroll me in that course.','37002','37001','degree_progress','CS02435 is available but is not the requested mathematics course.','math requested')

# 8 Priority / must vs prefer
ss=[section('38001','CS05500','01','Machine Learning',[],None,None,modality='online'),
    section('38002','CS05500','02','Machine Learning',['Monday','Wednesday'],'9:00 AM','10:15 AM',modality='in-person')]
b=base(sections=ss)
add('priority',1,b,'Enroll me in CS05500. Online is mandatory; morning is only a preference if possible.','38001','38002','requirement_priority','An in-person morning offering violates the mandatory online requirement.','online mandatory')
add('priority',2,b,'Enroll me in CS05500. Morning is mandatory; online is only a preference if possible.','38002','38001','requirement_priority','The online asynchronous option does not satisfy a required morning meeting.','morning mandatory')

# 9 Two simultaneous semantic constraints
ss=[section('39001','CS06600','01','Networks',['Monday','Wednesday'],'9:00 AM','10:15 AM',modality='online'),
    section('39002','CS06600','02','Networks',['Tuesday','Thursday'],'2:00 PM','3:15 PM',modality='online'),
    section('39003','CS06600','03','Networks',['Monday','Wednesday'],'2:00 PM','3:15 PM',modality='in-person'),
    section('39004','CS06600','04','Networks',['Tuesday','Thursday'],'9:00 AM','10:15 AM',modality='in-person')]
b=base(sections=ss)
add('compound_constraints',1,b,'I need CS06600 online AND in the afternoon. Both conditions are required.','39002','39003','compound_intent','39003 is afternoon but in-person; 39001 is online but morning.','online + afternoon')
add('compound_constraints',2,b,'I need CS06600 in-person AND in the morning. Both conditions are required.','39004','39001','compound_intent','39001 is morning but online; 39003 is in-person but afternoon.','in-person + morning')

# 10 Withdrawal/earned distinction
ss=[section('40001','CS04222','01','Data Structures',['Monday','Wednesday'],'11:00 AM','12:15 PM',credits=4),
    section('40002','CS05900','01','Software Project',['Tuesday','Thursday'],'11:00 AM','12:15 PM')]
reqs=[{'req_id':'data_structures','category':'Major Foundations','rule':'all_of',
       'course_options':['CS04222'],'title':'Data Structures', 'credit_hours':4,'minimum_grade':'C-'}]
withdrawn=[{'term':'Spring 2026','course_id':'CS04222','grade':'W','credit_hours':4}]
passed=[{'term':'Spring 2026','course_id':'CS04222','grade':'B','credit_hours':4}]
prompt='If I have not earned credit for CS04222, enroll me in it. If I have earned it already, enroll me in CS05900 instead.'
add('withdrawal_credit',1,base(sections=ss,completed=withdrawn,requirements=reqs),prompt,'40001','40002','transcript_credit_status','W is not earned credit according to Aneesh input specification.','W (unearned)')
add('withdrawal_credit',2,base(sections=ss,completed=passed,requirements=reqs),prompt,'40002','40001','transcript_credit_status','B is earned credit, so the alternate class is intended.','B (earned)')

# Write JSONL + summary
out=OUT/'semantic_cases_daquan_aneesh.jsonl'
with out.open('w',encoding='utf-8') as f:
    for c in cases:
        f.write(json.dumps(c,ensure_ascii=False,separators=(',',':'))+'\n')
summary={'total_cases':len(cases),'families':len(set(c['scenario_id'] for c in cases)),
         'categories':dict(Counter(c['semantic_dimension'] for c in cases))}
(OUT/'case_summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,indent=2))
