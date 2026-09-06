"""GraphQL documents, transcribed verbatim from the app's own requests.

Deliberately NOT hand-written. An earlier attempt reconstructed these from
observed response shapes and failed twice: once on a type (`patientId` is
`ID!`, not `String!`) and once by omitting fields we didn't know existed.
Using the app's exact documents guarantees we receive precisely what the
UI receives — including fields we'd never have guessed, like `Modalities`
(treatment modalities) and `Priority` on a goal.

Fragments are shared between operations exactly as the app shares them.
"""

# --- shared fragments -------------------------------------------------

ACTOR_VIEW = """
fragment ActorViewFragment on ActorViewModel {
  Id Title
  Name { Prefix FirstName MiddleName LastName Suffix }
  Photo { WebImageUrl }
  Roles ActorType
}
"""

ATTRIBUTION = """
fragment AttributionFragment on AttributionViewModel {
  DateTime Action
  Actor { ...ActorViewFragment }
}
"""

ACTOR_READ = """
fragment ActorReadFragment on ActorReadModel {
  Id ActorType Roles
  Name { Prefix FirstName MiddleName LastName Suffix }
  Photo { WebImageUrl }
}
"""

TASK_PERIOD = """
fragment TaskPeriodFragment on PeriodViewModel { StartDate EndDate }
"""

TIME_PERIOD = """
fragment TimePeriodFragment on TaskTimePeriodViewModel {
  Requested { ...TaskPeriodFragment }
}
"""

TASK = """
fragment TaskFragment on TaskViewModel {
  Id Name Deactivated Duration HealthConcernTypes PatientAssignable
  MeasurementDomain MeasurementType MeasurementUnits RecordCount
  TaskDescription TaskContentUri
  TimePeriod { ...TimePeriodFragment }
}
"""

MODALITY = """
fragment ModalityFragment on ModalityViewModel { Id Name GoalModalityId }
"""

SESSION_SETTING = """
fragment SessionSettingFragment on SessionSettingViewModel {
  Id Name GoalSessionId
}
"""

MEASUREMENT_COMPONENT = """
fragment MeasurementComponentValueFragment on MeasurementComponentValueViewModel {
  MeasurementTypeComponentId Value
}
"""

GOAL_TARGET = """
fragment GoalTargetFragment on GoalTargetViewModel {
  MeasurementTypeId EndDate Id
  Attribution { ...AttributionFragment }
  Components { ...MeasurementComponentValueFragment }
}
"""

BASELINE_MEASUREMENT = """
fragment BaselineMeasurementFragment on BaselineMeasurementViewModel {
  id MeasurementTypeId StartDate SourceFHIRUri
  Components { ...MeasurementComponentValueFragment }
}
"""

# Note `Modalities` (what the UI labels "Treatment modalities") and
# `Priority` / `AchievementStatus` — none of which were in the
# reconstructed query.
GOAL = """
fragment GoalFragment on GoalViewModel {
  Name TemplateId Description PatientStatement BaselineDescription
  EndDate ReviewDueDate Id Ordinal StartDate Created CreatedBy Continuous
  MedicalCodes { Code System }
  Progress AchievementStatus LifecycleStatus AchievementStatusReason
  AchievementStatusCreated
  AchievementStatusCreatedBy { ...ActorViewFragment }
  Attribution { ...AttributionFragment }
  IsActive IsPriority Priority
  Statuses { System Code }
  Modalities { ...ModalityFragment }
  SessionSettings { ...SessionSettingFragment }
  Tasks { ...TaskFragment }
  TaskSchedules
  AllGoalTargets(patientId: $patientId, healthConcernId: $healthConcernId) {
    MeasurementType { ...GoalTargetFragment }
    Milestones { ...GoalTargetFragment }
  }
  BaselineMeasurements { ...BaselineMeasurementFragment }
}
"""

ICD_DATA = """
fragment ICDDataFragment on ICDDataViewModel {
  Description FullDescription PLCDescription UnstrippedDiagnosisCode
}
"""

DIAGNOSIS = """
fragment DiagnosisFragment on DiagnosisViewModel {
  DiagnosisCode DiagnosisCodeSystem FHIRResourceId
  ICDData { ...ICDDataFragment }
}
"""

PROBLEM = """
fragment ProblemFragment on ProblemViewModel {
  ConcernStatus Description Id
  MedicalCodes { System Code }
  StartDate CreatedBy Note
  HCNotes { HCNoteId Content }
  Diagnoses { ...DiagnosisFragment }
  Symptom
  Observations { TypeName Summary Archived }
  PlanHCMappings { PlanHCMapId }
  Attribution { ...AttributionFragment }
}
"""

TASK_SCHEDULE = """
fragment TaskScheduleFragment on TaskScheduleViewModel {
  Id id Ordinal
  AssignedTask { ...TaskFragment }
  AssignedTaskClass HasHadWeeklyTaskScheduleItems
  HashHadUntilCompleteTaskScheduleItems
  Note IsDeactivated Priority Target CreatedBy Created TaskStatus TemplateId
  TimePeriod { ...TimePeriodFragment }
  UntilCompleteTaskScheduleItems {
    Id DueDate ExpirationType StartType
    Repeats { Period Interval }
    ScheduledTime ScheduledTimePeriod
  }
  WeeklyScheduleItems {
    Id DayOfWeek StartType StopType ScheduledTime ScheduledTimePeriod
  }
  AssignedActors { ...ActorReadFragment }
  GoalIds
  Attribution { ...AttributionFragment }
  TaskId RolesAllowedToRecordOutcome
}
"""

PROBLEM_GOAL_LINK = """
fragment ProblemGoalLinkFragment on ProblemGoalLinkViewModel {
  Id GoalId ProblemId CreatedDateTime LastModifiedDateTime
}
"""

HEALTH_CONCERN = """
fragment HealthConcernFragment on HealthConcernViewModel {
  AddedDateTime AddedBy ReviewDueDate
  Attribution { ...AttributionFragment }
  CarePlanIds ClinicalEncounterIds IsArchived
  CarePlans { Schedules { ...TaskScheduleFragment } }
  Goals { ...GoalFragment }
  Id Name HealthConcernType PatientId
  Problems { ...ProblemFragment }
  ProblemGoalLinks { ...ProblemGoalLinkFragment }
}
"""

# Every fragment HealthConcernFragment transitively depends on.
_CONCERN_DEPS = (
    ACTOR_VIEW + ATTRIBUTION + ACTOR_READ + TASK_PERIOD + TIME_PERIOD + TASK
    + MODALITY + SESSION_SETTING + MEASUREMENT_COMPONENT + GOAL_TARGET
    + BASELINE_MEASUREMENT + GOAL + ICD_DATA + DIAGNOSIS + PROBLEM
    + TASK_SCHEDULE + PROBLEM_GOAL_LINK + HEALTH_CONCERN
)

# --- operations -------------------------------------------------------

QUERIES = {
    # The workhorse. Returns every health concern with its goals (including
    # Modalities and Priority), its problems (with diagnoses and
    # observations) AND CarePlans.Schedules — which is objectives and
    # interventions. Most of an extraction comes from this one call.
    "GetPatientCarePlanInternal": """
query GetPatientCarePlanInternal($patientId: ID!, $healthConcernId: ID, $additionalTypes: [String], $inpatientStayId: ID) {
  getPatientCarePlanInternal(patientId: $patientId, additionalTypes: $additionalTypes, inpatientStayId: $inpatientStayId) {
    PatientId
    HealthConcerns { ...HealthConcernFragment }
  }
}
""" + _CONCERN_DEPS,

    # Same fragment, scoped to one concern. Needs distributorId/sponsorId.
    "GetHealthConcern": """
query GetHealthConcern($distributorId: ID!, $sponsorId: ID!, $patientId: ID!, $healthConcernId: ID!) {
  getHealthConcern(distributorId: $distributorId, sponsorId: $sponsorId, patientId: $patientId, healthConcernId: $healthConcernId) {
    ...HealthConcernFragment
  }
}
""" + _CONCERN_DEPS,

    # Goal progress history. startDate/endDate are String! (non-null) —
    # declaring them nullable is rejected with "Variable $startDate of
    # type String used in position expecting type String!".
    "GetGoalStatusHistoryInternal": """
query GetGoalStatusHistoryInternal($patientId: ID!, $healthConcernId: ID!, $goalId: ID!, $startDate: String!, $endDate: String!) {
  getGoalStatusHistoryInternal(patientId: $patientId, healthConcernId: $healthConcernId, goalId: $goalId, startDate: $startDate, endDate: $endDate) {
    AchievementStatusHistory {
      Status DateTimeAdded StatusReason
      Actor { ...ActorViewFragment }
    }
    LifecycleStatusHistory {
      Status DateTimeAdded StatusReason
      Actor { ...ActorViewFragment }
    }
  }
}
""" + ACTOR_VIEW,

    # goalId is NULLABLE — omitting it returns task schedules for EVERY
    # goal in the concern in one request. That is what collapses the six
    # sequential goal expansions (~29s on the DOM path) into a single call.
    "GetTaskSchedulesWithScheduledTasks": """
query GetTaskSchedulesWithScheduledTasks($patientId: ID!, $healthConcernId: ID!, $startDate: String!, $endDate: String!, $goalId: ID) {
  getTaskSchedulesWithScheduledTasks(patientId: $patientId, healthConcernId: $healthConcernId, startDate: $startDate, endDate: $endDate, goalId: $goalId) {
    TaskSchedules {
      TaskSchedule {
        Id GoalIds TaskStatus IsDeactivated Ordinal Note Priority
        AssignedTask { Id Name TaskDescription HealthConcernTypes PatientAssignable MeasurementType }
        AssignedTaskClass RolesAllowedToRecordOutcome AssignedActors { ...ActorReadFragment }
        TimePeriod { Requested { StartDate EndDate } }
        Attribution { ...AttributionFragment }
      }
      DayOfCare
    }
  }
}
""" + ACTOR_VIEW + ATTRIBUTION + ACTOR_READ,

    "GetAllHealthConcernAttestationsInternal": """
query GetAllHealthConcernAttestationsInternal($patientId: ID!, $healthConcernId: ID!) {
  getAllHealthConcernAttestationsInternal(patientId: $patientId, healthConcernId: $healthConcernId) {
    ...AttestationFragment
  }
}
fragment AttestationFragment on AttestationViewModel {
  Id Status PlanName PdfLink PatientAttestedStatus PatientAttestedDate
  PatientDisplayName
  PatientName { FirstName FirstNameUsed MiddleName LastName Suffix }
  ProviderId ProviderDisplayName PatientId PatientMessageRequestId
  PatientMessageResult { Method Contact ShortName Description ResultCodeId }
  ProviderAttestations {
    Id PlanAttestationId ProviderId ProviderAttestedStatus
    ProviderAttestedNote ProviderAttestedDate ProviderDisplayName
    Created { ...AttributionFragment }
    Updated { ...AttributionFragment }
  }
  Esignature PortalUserRelationshipId PortalThirdPartyUserId
  Created { ...AttributionFragment }
  Updated { ...AttributionFragment }
  Deleted { ...AttributionFragment }
  LastDownloaded
  LastDownloadedBy { ...ActorViewFragment }
}
""" + ACTOR_VIEW + ATTRIBUTION,

    # Believed to back "client characteristics" — TypeName looks like the
    # grouping (strengths/needs/abilities/preferences/supports).
    "GetObservations": """
query GetObservations($patientId: ID!) {
  getObservations(patientId: $patientId) {
    ObservationId TypeName Summary Archived
    HealthConcerns { HealthConcernId SnomedCode }
    Created
    CreatedBy { Id FirstName LastName Suffix }
    LastModified
    LastModifiedBy { Id FirstName LastName Suffix }
  }
}
""",

    "GetFHIRConditions": """
query GetFHIRConditions($enterpriseId: ID!, $chartSharingGroupId: ID!, $cursor: String) {
  getFHIRConditions(enterpriseId: $enterpriseId, chartSharingGroupId: $chartSharingGroupId, cursor: $cursor) {
    type timestamp
    link { relation url }
    entry {
      fullUrl
      resource {
        id
        meta { lastUpdated profile }
        clinicalStatus { ...CodeableConceptFragment }
        verificationStatus { ...CodeableConceptFragment }
        category { ...CodeableConceptFragment }
        code { ...CodeableConceptFragment }
        onsetDateTime abatementDateTime recordedDate
      }
    }
    resourceType
  }
}
fragment CodeableConceptFragment on CodeableConcept {
  coding { system code display }
}
""",

    "GetPatientInternal": """
query GetPatientInternal($patientId: ID!) {
  getPatientInternal(patientId: $patientId) {
    Id DisplayName FirstName FirstNameUsed LastName BirthDate Sex
    Address1 Address2 City State PostalCode
    EmailAddress HomePhone MobilePhone
    Name { Prefix Suffix }
    Status IsActivated SponsorId TimeZoneId UserName
  }
}
""",

    "GetSessionSettings": """
query GetSessionSettings {
  getSessionSettings { SessionSettings { Id Name } }
}
""",
}
