import sys
import numpy as np
import pandas as pd
import sqlite3
import math
from itertools import permutations


# connect to course registration database for checks
conn = sqlite3.connect("example_registration.db")
cursor = conn.cursor()

# indicates which course fields are more important (if any)
# corresponds to: course, section, time, prof, campus, and credits, respectively
devWeights = [3, 0, 2, 1, 1, 0]


# finds the deviation between the specified courses along several axes
def get_deviation(expectedCourse, proposedCourse) -> float:

    expectedCourseData = pd.DataFrame.to_numpy(pd.read_sql_query("SELECT * FROM courses WHERE key = " + str(expectedCourse), conn))[0]
    proposedCourseData = pd.DataFrame.to_numpy(pd.read_sql_query("SELECT * FROM courses WHERE key = " + str(proposedCourse), conn))[0]

    out = np.array([], dtype=float)

    # compare course numbers (1 if the same, 0.3 if the same subject, 0 otherwise)
    out = np.append(out,
        1 if (expectedCourseData[1] == proposedCourseData[1]) else
        0.3 if (expectedCourseData[1].rstrip('0123456789') == proposedCourseData[1].rstrip('0123456789')) else 0
    )

    # compare section
    out = np.append(out, 1 if (expectedCourseData[2] == proposedCourseData[2]) else (1.0 / abs(expectedCourseData[2] - proposedCourseData[2])))

    # compare time (assumes that the time of the expected course is prefered, not just that a conflict is avoided)
    out = np.append(out,
        1 if (expectedCourseData[3] == None and proposedCourseData[3] == None) else
        0 if (expectedCourseData[3] == None or proposedCourseData[3] == None) else
        1 if (expectedCourseData[3] == proposedCourseData[3] and expectedCourseData[0] == proposedCourseData[0]) else
        0 if (expectedCourseData[3] == proposedCourseData[3]) else
        (1 / max(abs(expectedCourseData[3].split("-")[1] - proposedCourseData[3].split("-")[1]), 1)) if (expectedCourseData[3].rstrip('0123456789') == proposedCourseData[3].rstrip('0123456789')) else
        0
    )

    # compares prof
    out = np.append(out, 1 if (expectedCourseData[4] == proposedCourseData[4]) else 0)

    # compares campus
    out = np.append(out, 1 if (expectedCourseData[5] == proposedCourseData[5]) else 0)

    # compares credits
    out = np.append(out, (expectedCourseData[6] / proposedCourseData[6]) if (expectedCourseData[6] - proposedCourseData[6] > 0) else (proposedCourseData[6] / expectedCourseData[6]))

    # get the weighted average of the categorical deviations
    sum = 0.0
    count = 0
    for val in out:
        sum += (val * devWeights[count])
        count += 1
    sum /= count
    return sum


# takes in two sets of section_id values and returns a confidence score [0,1]
# based on how well they match
def compare_to_ground_truth(expected, proposed) -> float:
    # count how many expected courses are present
    numSame = 0
    for exp in range(expected.size - 1, -1, -1):
        if (np.equal(expected[exp], proposed[exp])):
            proposed = np.delete(proposed, np.where(proposed == expected[exp]))
            expected = np.delete(expected, exp)
            numSame += 1

    if (expected.size == 0 and proposed.size == 0): return 1

    # If proposed contains expected, confidence is only reduced by extra courses
    if (expected.size == 0): return (float)(numSame) / (numSame + proposed.size)

    # If proposed has too few courses but all match with expected,
    # return the fraction of courses contained
    if (proposed.size == 0): return (float)(numSame) / (numSame + expected.size)

    # If both proposed and expected still contain courses, try to find the 'best'
    # mapping of proposed onto expected
    # The deviation between two respective courses is represented by:
    # [float: timeDev, float: subjectDev], which is then condensed into one number
    # counterintuitively, 1 is a lower deviation than 0.5, so really lowestDev is being maximized
    lowestDev = 0.0
    bestPerm = None

    constantList = None
    permList = None
    listSize = 0
    if (expected.size < proposed.size):
        constantList = expected
        permList = proposed
        listSize = proposed.size
    else:
        constantList = proposed
        permList = expected
        listSize = expected.size

    # the number of ways the larger list can be sampled to one-to-one map to the smaller
    for perm in (permutations(permList, listSize)):
        # currently just takes an average of all deviations
        # could be changed in future to value some courses over others
        currentDev = numSame
        count = 0
        for course in perm:
            currentDev += get_deviation(course, constantList[count])
        currentDev /= len(perm) + numSame

        # update if necessary
        if currentDev > lowestDev:
            lowestDev = currentDev
            bestPerm = perm
    return lowestDev

# example tests
def main():

    print("Test #1:")
    print((12345, 'CS04113', 1, 'MW 0930-1045', 'Chein, Chia', 'Glassboro', 4)) 
    print((24680, 'CS04114', 1, 'MW 1230-1345', 'Ambati, Sushanth', 'Glassboro', 3))
    print((13579, 'CS06205', 2, 'TR 0800-0915', 'Ivanov, Nick', 'Glassboro', 3))
    test1 = np.array([[12345, 24680, 13579], [12345, 24680, 13579]])
    print(compare_to_ground_truth(test1[0], test1[1]))
    print()

    print("Test #2:")
    print((98765, 'CS04215', 1, 'MW 0800-1045', 'Provine, Darren', 'Glassboro', 3))
    print((19469, 'CMS04321', 1, 'MWF 0800-0915', 'TBD', 'Glassboro', 3))
    test2 = np.array([[12345, 24680, 13579, 98765], [12345, 24680, 13579, 19469]])
    print(compare_to_ground_truth(test2[0], test2[1]))
    print()

    print("Test #3:")
    print((12345, 'CS04113', 1, 'MW 0930-1045', 'Chein, Chia', 'Glassboro', 4))
    print((12346, 'CS04113', 2, 'MW 0945-1100', 'Chein, Chia', 'Glassboro', 3))
    test3 = np.array([[12345], [12346]])
    print(compare_to_ground_truth(test3[0], test3[1]))
    print()

    print("Test #4:")
    print((24680, 'CS04114', 1, 'MW 1230-1345', 'Ambati, Sushanth', 'Glassboro', 3))
    print((24681, 'CS04114', 1, 'MW 1230-1345', 'Myers, Jack', 'Glassboro', 3))
    test4 = np.array([[24680], [24681]])
    print(compare_to_ground_truth(test4[0], test4[1]))
    print()

    print("Test #5:")
    print((13579, 'CS06205', 2, 'TR 0800-0915', 'Ivanov, Nick', 'Glassboro', 3))
    print((13580, 'CS06205', 2, 'NULL', 'Ivanov, Nick', 'ONLINE', 3))
    test5 = np.array([[13579], [13580]])
    print(compare_to_ground_truth(test5[0], test5[1]))
    print()

    print("Test #6:")
    print((12345, 'CS04113', 1, 'MW 0930-1045', 'Chein, Chia', 'Glassboro', 4))
    print((98765, 'CS04215', 1, 'MW 0800-1045', 'Provine, Darren', 'Glassboro', 3))
    test6 = np.array([[12345], [98765]])
    print(compare_to_ground_truth(test6[0], test6[1]))
    print()

    print("Test #7:")
    print((12345, 'CS04113', 1, 'MW 0930-1045', 'Chein, Chia', 'Glassboro', 4))
    print((19471, 'CMS04321', 3, 'MWF 1100-1230', 'TBD', 'ONLINE', 3))
    test7 = np.array([[12345], [19471]])
    print(compare_to_ground_truth(test7[0], test7[1]))
    print()


if __name__ == "__main__":
    main()