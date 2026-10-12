DROP TABLE IF EXISTS courses;
DROP TABLE IF EXISTS seats;

CREATE TABLE courses (
    -- CRN (unique identifier)
    key INTEGER PRIMARY KEY,
    -- course number (i.e. CS06889)
    courseNum TEXT NOT NULL,
    -- section number
    sectionNum INTEGER NOT NULL,
    -- meeting times (i.e. MW 12:30-13:45)
    meetingTime TEXT,
    -- professor name
    prof TEXT NOT NULL,
    -- campus
    campus TEXT NOT NULL,
    -- num credits
    numCredits INTEGER
);

INSERT INTO courses VALUES
    (12345, 'CS04113', 1, 'MW 0930-1045', 'Chein, Chia', 'Glassboro', 4),
    (12346, 'CS04113', 2, 'MW 0945-1100', 'Chein, Chia', 'Glassboro', 3),
    (24680, 'CS04114', 1, 'MW 1230-1345', 'Ambati, Sushanth', 'Glassboro', 3),
    (24681, 'CS04114', 1, 'MW 1230-1345', 'Myers, Jack', 'Glassboro', 3),
    (13579, 'CS06205', 2, 'TR 0800-0915', 'Ivanov, Nick', 'Glassboro', 3),
    (13580, 'CS06205', 2, NULL, 'Ivanov, Nick', 'ONLINE', 3),
    (98765, 'CS04215', 1, 'MW 0800-1045', 'Provine, Darren', 'Glassboro', 3),
    (19469, 'CMS04321', 1, 'MWF 0800-0915', 'TBD', 'Glassboro', 3),
    (19470, 'CMS04321', 2, 'MWF 0930-1045', 'TBD', 'Glassboro', 3),
    (19471, 'CMS04321', 3, 'MWF 1100-1230', 'TBD', 'ONLINE', 3);

CREATE TABLE seats (
    -- CRN
    key INTEGER PRIMARY KEY,
    -- number of seats left in the class
    value INTEGER NOT NULL
);

INSERT INTO seats VALUES
    (12345, 1),
    (12346, 10),
    (24680, 3),
    (24681, 4),
    (13579, 0),
    (13580, 15),
    (98765, 2),
    (19469, 5),
    (19470, 7),
    (19471, 12);