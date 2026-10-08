# Registration Input Format Specification

## Purpose
This specification defines the JSON input format used by the registration agent. The input contains information about the student, transcript, degree program requirements, available course sections, and the student's registration request.

The format is designed so that the registration agent can use the provided information to determine whether a requested course or section is appropriate and whether the requested registration is possible.

## Top-level JSON structure
{
  "student": {
    "student_id": "S001",
  "degree": "Bachelor of Science",
  "major": "Computer Science",
  "minor": "Mathematics",
  "campus": "Glassboro"

  },
  "transcript": {
    "completed_courses":[
        {
        "term": "Spring 2026",
        "course_id": "CS04113",
        "campus":"Glassboro",
        "level": "UG",
        "title" : "INTRO OBJ-ORIENT PRGRMMNG",
        "grade": "A+",
        "credit_hours": 4,
        "quality_hours": "16.00"
        }
    ],
        "courses_in_progress":[
            {
        "term": "Fall 2026",
        "course_id": "CS04114",
        "campus": "Glassboro",
        "level": "UG",
        "title" : "OBJ-ORIENT PRGRM/DATA ABSTR",
        "credit_hours": 4
            }
        ]

  },
  "program_requirements": [
    {
    "category": "Rowan Core: Communicative Literacy",
    "course_options": ["COMP01111"],
    "title": "College Composition I",
    "credit_hours": 3,
    "minimum_grade": "D-",
    "notes": "Required course"
    },
    {
      "category": "Major Requirements: Foundational Courses",
      "course_options": ["CS04113"],
      "title": "Introduction to Object-Oriented Programming",
      "credit_hours": 4,
      "minimum_grade": "C-",
      "notes": "Students must be ready for MATH 01130"
    },
    {
      "category": "Major Requirements: Computer Science Restricted Electives",
      "course_options": [
        "CS01303", "CS01395", "CS01400", "CS02435", "CS02480", "CS03351", "CS07450"
      ],
      "title": "Computer Science Restricted Electives Block",
      "credit_hours": 12,
      "minimum_grade": "D-",
      "notes": "Choose 12 credits from the listed elective courses"
    }
  ],
  "schedule": {
    "term": "Fall 2026",
  "courses": [
    {
      "course_id": "CS05000",
      "section": "01",
      "title": "Introduction to Software Engineering",
      "crn": "12345",
      "credits": 3,
      "seats_total":30,
      "seats_taken":25,
      "modality" : "in-person",
      "campus": "Glassboro",
      "meetings": [
        {
          "days": ["Monday", "Wednesday"],
          "start_time": "10:00 AM",
          "end_time": "11:15 AM",
          "type": "Lecture"
        }
      ]
    },
    {
      "course_id": "MATH03000",
      "section": "02",
      "title": "Applied Mathematics",
      "crn": "12346",
      "credits": 3,
       "seats_total":30,
      "seats_taken":25,
      "modality" : "in-person",
      "campus": "Glassboro",
      "meetings": [
        {
          "days": ["Tuesday", "Thursday"],
          "start_time": "1:00 PM",
          "end_time": "2:15 PM",
          "type": "Lecture"
        }
      ]
    },
    {
      "course_id": "CS06000",
      "section": "01",
      "title": "Database Systems",
      "crn": "12347",
      "credits": 3,
       "seats_total":30,
      "seats_taken":25,
      "modality" : "in-person",
      "campus": "Glassboro",
      "meetings": [
        {
          "days": ["Monday", "Wednesday"],
          "start_time": "2:00 PM",
          "end_time": "3:15 PM",
          "type": "Lecture"
        }
      ]
    }
  ]

  },
  "prompt": "Enroll me in CS05000."
}
## Student

Describe what goes inside "student".

Student

The student object contains basic information about the student's academic program.

Field	     Type	    Required	       Description
student_id	string	     Required	Unique identifier for the student
degree	    string	    Required	     Degree being pursued
major	    string	     Required	      Student's major
minor	    string	      Optional	       Student's minor



## Transcript

Describe what goes inside "transcript".

The transcript object contains the student's completed and currently in-progress courses. Completed courses include the term, subject, campus, course number, level, title, grade, and credit hours. In-progress courses contain the current course information and credit hours.

## Program Requirements

Describe what goes inside "program_requirements".

The program_requirements array contains the requirements the student must satisfy for their degree program. Each requirement specifies a category, eligible course options, title, credit hours, minimum grade, and additional notes.

## Schedule

Describe what goes inside "schedule".

The schedule object contains courses and sections available for a particular term. Each course includes its course ID, section, title, CRN, credits, campus, and meeting times. Meeting information includes the days, start and end times, and type of meeting.



## Prompt

Describe what goes inside "prompt".

The prompt contains the student's natural-language registration request, such as requesting enrollment in a particular course.

