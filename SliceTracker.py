import EditorLib, DICOMLib
import csv, re, numpy
import os, sys, shutil, datetime, logging
import slicer, ctk, vtk, qt

from Editor import EditorWidget
from SliceTrackerUtils.Constants import DICOMTAGS, COLOR, STYLE, SliceTrackerConstants
from SliceTrackerUtils.RegistrationData import RegistrationResults, RegistrationResult
from SliceTrackerUtils.ZFrameRegistration import ZFrameRegistration
from SliceTrackerUtils.helpers import SliceAnnotation, ExtendedQMessageBox
from SliceTrackerUtils.helpers import WatchBoxAttribute, BasicInformationWatchBox, DICOMBasedInformationWatchBox
from SliceTrackerUtils.mixins import ModuleWidgetMixin, ModuleLogicMixin
from slicer.ScriptedLoadableModule import *


class SliceTracker(ScriptedLoadableModule):

  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = "SliceTracker"
    self.parent.categories = ["Radiology"]
    self.parent.dependencies = ["mpReview", "mpReviewPreprocessor"]
    self.parent.contributors = ["Peter Behringer (SPL), Christian Herz (SPL), Andriy Fedorov (SPL)"]
    self.parent.helpText = """ SliceTracker facilitates support of MRI-guided targeted prostate biopsy. """
    self.parent.acknowledgementText = """Surgical Planning Laboratory, Brigham and Women's Hospital, Harvard
                                          Medical School, Boston, USA This work was supported in part by the National
                                          Institutes of Health through grants U24 CA180918,
                                          R01 CA111288 and P41 EB015898."""


class SliceTrackerWidget(ScriptedLoadableModuleWidget, ModuleWidgetMixin, SliceTrackerConstants):

  @property
  def registrationResults(self):
    return self.logic.registrationResults

  @property
  def currentResult(self):
    return self.registrationResults.activeResult

  @currentResult.setter
  def currentResult(self, series):
    self.registrationResults.activeResult = series

  @property
  def caseRootDir(self):
    return self.casesRootDirectoryButton.directory

  @caseRootDir.setter
  def caseRootDir(self, path):
    exists = os.path.exists(path)
    self.createNewCaseButton.enabled = exists
    self.openCaseButton.enabled = exists
    self.setSetting('CasesRootLocation', path if exists else None)
    self.casesRootDirectoryButton.text = self.truncatePath(path) if exists else "Choose output directory"
    self.casesRootDirectoryButton.toolTip = path

  @property
  def preopDataDir(self):
    return self._preopDataDir

  @preopDataDir.setter
  def preopDataDir(self, path):
    slicer.mrmlScene.Clear(0)
    self.logic.resetAndInitializeData()
    self.updateViewSettingButtons()
    self.removeSliceAnnotations()
    self._preopDataDir = path
    self.setSetting('PreopLocation', path)
    self.loadPreopData()
    self.intraopSeriesSelector.clear()
    self.trackTargetsButton.setEnabled(False)
    self.targetTable.enabled = True

  @property
  def intraopDataDir(self):
    return self.logic.intraopDataDir

  @intraopDataDir.setter
  def intraopDataDir(self, path):
    self.intraopWatchBox.sourceFile = None
    self.logic.setReceivedNewImageDataCallback(self.onNewImageDataReceived)
    self.logic.intraopDataDir = path
    self.logic.startStoreSCP(slicer.util.warningDisplay)

  @property
  def evaluationMode(self):
    return self._evaluationMode

  @evaluationMode.setter
  def evaluationMode(self, value):
    self._evaluationMode = value
    if value is False:
      self.registrationAssessmentMode = False
      self.disableTargetMovingMode()
      self.targetTable.disconnect('doubleClicked(QModelIndex)', self.onMoveTargetRequest)
    else:
      self.targetTable.connect('doubleClicked(QModelIndex)', self.onMoveTargetRequest)

  @property
  def mpReviewPreprocessedOutput(self):
    return os.path.join(self.currentCaseDirectory, "mpReviewPreprocessed") if self.currentCaseDirectory else None

  @property
  def preopDICOMDataDirectory(self):
    return os.path.join(self.currentCaseDirectory, "DICOM", "Preop") if self.currentCaseDirectory else None

  @property
  def outputDir(self):
    return os.path.join(self.currentCaseDirectory, "SliceTrackerOutputs")

  def __init__(self, parent=None):
    ScriptedLoadableModuleWidget.__init__(self, parent)
    self.logic = SliceTrackerLogic()
    self.markupsLogic = slicer.modules.markups.logic()
    self.volumesLogic = slicer.modules.volumes.logic()
    self.annotationLogic = slicer.modules.annotations.logic()
    self.modulePath = os.path.dirname(slicer.util.modulePath(self.moduleName))
    self.iconPath = os.path.join(self.modulePath, 'Resources/Icons')
    self.setupIcons()

  def onReload(self):
    try:
      if self.currentCaseDirectory:
        self.closeCase()
      self.removeSliceAnnotations()
      self.clearTargetMovementObserverAndAnnotations()
      self.resetVisualEffects()
    except:
      pass
    ScriptedLoadableModuleWidget.onReload(self)

  def cleanup(self):
    ScriptedLoadableModuleWidget.cleanup(self)
    self.removeSliceAnnotations()
    self.clearTargetMovementObserverAndAnnotations()
    self.disconnectCrosshairNode()

  def updateOutputFolder(self):
    if self.outputDir and not os.path.exists(self.outputDir):
      self.logic.createDirectory(self.outputDir)
    if os.path.exists(self.outputDir) and self.patientWatchBox.getInformation("PatientID") != '' \
            and self.intraopWatchBox.getInformation("StudyDate") != '':
      time = qt.QTime().currentTime().toString().replace(":", "")
      date = str(qt.QDate().currentDate())
      finalDirectory = self.patientWatchBox.getInformation("PatientID") + "-biopsy-" + date + "-" + time
      self.generatedOutputDirectory = os.path.join(self.outputDir, finalDirectory, "MRgBiopsy")
      self.caseCompletedButton.enabled = True
      self.collapsibleDirectoryConfigurationArea.collapsed = True
    else:
      self.generatedOutputDirectory = ""
      self.caseCompletedButton.enabled = False

  def createPatientWatchBox(self):
    watchBoxInformation = [WatchBoxAttribute('PatientID', 'Patient ID: ', DICOMTAGS.PATIENT_ID),
                           WatchBoxAttribute('PatientName', 'Patient Name: ', DICOMTAGS.PATIENT_NAME),
                           WatchBoxAttribute('DOB', 'Date of Birth: ', DICOMTAGS.PATIENT_BIRTH_DATE),
                           WatchBoxAttribute('StudyDate', 'Preop Study Date: ', DICOMTAGS.PATIENT_ID)]
    self.patientWatchBox = DICOMBasedInformationWatchBox(watchBoxInformation)
    self.layout.addWidget(self.patientWatchBox)

    intraopWatchBoxInformation = [WatchBoxAttribute('StudyDate', 'Intraop Study Date: ', DICOMTAGS.STUDY_DATE),
                                  WatchBoxAttribute('CurrentSeries', 'Current Series: ', [DICOMTAGS.SERIES_NUMBER,
                                                                                          DICOMTAGS.SERIES_DESCRIPTION])]
    self.intraopWatchBox = DICOMBasedInformationWatchBox(intraopWatchBoxInformation)
    self.registrationDetailsButton = self.createButton("", icon=self.settingsIcon, styleSheet="border:none;",
                                                       maximumWidth=16)
    self.layout.addWidget(self.intraopWatchBox)

  def createCaseInformationArea(self):
    self.casesRootDirectoryButton = self.createDirectoryButton(text="Choose cases root location",
                                                               caption="Choose cases root location",
                                                               directory=self.getSetting('CasesRootLocation'))
    self.createCaseWatchBox()
    self.collapsibleDirectoryConfigurationArea = ctk.ctkCollapsibleButton()
    self.collapsibleDirectoryConfigurationArea.collapsed = True
    self.collapsibleDirectoryConfigurationArea.text = "Case Directory Settings"
    self.directoryConfigurationLayout = qt.QGridLayout(self.collapsibleDirectoryConfigurationArea)
    self.directoryConfigurationLayout.addWidget(qt.QLabel("Cases Root Directory"), 1, 0, 1, 1)
    self.directoryConfigurationLayout.addWidget(self.casesRootDirectoryButton, 1, 1, 1, 1)
    self.directoryConfigurationLayout.addWidget(self.caseWatchBox, 2, 0, 1, qt.QSizePolicy.ExpandFlag)
    self.layout.addWidget(self.collapsibleDirectoryConfigurationArea)

  def createCaseWatchBox(self):
    watchBoxInformation = [WatchBoxAttribute('CurrentCaseDirectory', 'Directory'),
                           WatchBoxAttribute('CurrentPreopDICOMDirectory', 'Preop DICOM Directory: '),
                           WatchBoxAttribute('CurrentIntraopDICOMDirectory', 'Intraop DICOM Directory: '),
                           WatchBoxAttribute('mpReviewDirectory', 'mpReview Directory: ')]
    self.caseWatchBox = BasicInformationWatchBox(watchBoxInformation, title="Current Case")

  def setupIcons(self):
    self.cancelSegmentationIcon = self.createIcon('icon-cancelSegmentation.png')
    self.greenCheckIcon = self.createIcon('icon-greenCheck.png')
    self.quickSegmentationIcon = self.createIcon('icon-quickSegmentation.png')
    self.newImageDataIcon = self.createIcon('icon-newImageData.png')
    self.settingsIcon = self.createIcon('icon-settings.png')
    self.undoIcon = self.createIcon('icon-undo.png')
    self.redoIcon = self.createIcon('icon-redo.png')
    self.fourUpIcon = self.createIcon('icon-four-up.png')
    self.sideBySideIcon = self.createIcon('icon-side-by-side.png')
    self.crosshairIcon = self.createIcon('icon-crosshair')
    self.zFrameIcon = self.createIcon('icon-zframe')
    self.needleIcon = self.createIcon('icon-needle')
    self.templateIcon = self.createIcon('icon-template')
    self.pathIcon = self.createIcon('icon-path')
    self.revealCursorIcon = self.createIcon('icon-revealCursor')
    self.skipIcon = self.createIcon('icon-skip')

  def setup(self):
    ScriptedLoadableModuleWidget.setup(self)

    try:
      import VolumeClipWithModel
    except ImportError:
      return slicer.util.warningDisplay("Error: Could not find extension VolumeClip. Open Slicer Extension Manager and install "
                                "VolumeClip.", "Missing Extension")

    self.ratingWindow = RatingWindow(maximumValue=5)
    self.sliceAnnotations = []
    self.mouseReleaseEventObservers = {}
    self.revealCursor = None
    self.currentTargets = None
    self.moveTargetMode = False
    self.currentlyMovedTargetModelIndex = None

    self.crosshairNode = None
    self.crosshairNodeObserverTag = None

    self.logic.retryMode = False
    self.logic.zFrameRegistrationSuccessful = False

    self.lastSelectedModelIndex = None

    self.notifyUserAboutNewData = True

    self.createPatientWatchBox()
    self.createCaseInformationArea()
    self.setupRegistrationWatchBox()
    self.settingsArea()

    self.setupSliceWidgets()
    self.setupZFrameRegistrationUIElements()
    self.setupTargetingStepUIElements()
    self.setupSegmentationUIElements()
    self.setupEvaluationStepUIElements()

    self.setupConnections()

    self.generatedOutputDirectory = ""
    self.caseRootDir = self.getSetting('CasesRootLocation')
    self.currentCaseDirectory = None

    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)
    self.setAxialOrientation()

    self.showAcceptRegistrationWarning = False

    self.registrationAssessmentMode = False
    self.evaluationMode = False

    self.roiObserverTag = None
    self.coverTemplateROI = None
    self.zFrameCroppedVolume = None
    self.zFrameLabelVolume = None
    self.zFrameMaskedVolume = None

    self.zFrameClickObserver = None
    self.zFrameInstructionAnnotation = None

  def settingsArea(self):
    self.collapsibleSettingsArea = ctk.ctkCollapsibleButton()
    self.collapsibleSettingsArea.text = "Settings"
    self.collapsibleSettingsArea.collapsed = True
    self.settingsAreaLayout = qt.QGridLayout(self.collapsibleSettingsArea)

    self.setupViewSettingGroupBox()
    self.setupZFrameViewSettingsGroupBox()

    self.settingsAreaLayout.addWidget(self.zFrameViewSettingsGroupBox)
    self.layout.addWidget(self.collapsibleSettingsArea)

  def setupViewSettingGroupBox(self):
    self.setupLayoutsButton()
    self.setupCrosshairButton()
    self.viewSettingsGroupBox = qt.QGroupBox('View options:')
    viewSettingsLayout = qt.QVBoxLayout()
    self.viewSettingsGroupBox.setLayout(viewSettingsLayout)

  def setupLayoutsButton(self):
    self.layoutsMenuButton = self.createButton("Layouts", minimumHeight=30)
    self.layoutsMenu = qt.QMenu()
    self.layoutDict = dict()
    self.layoutDict[self.LAYOUT_SIDE_BY_SIDE] = self.layoutsMenu.addAction(self.sideBySideIcon, "Side by side")
    self.layoutDict[self.LAYOUT_FOUR_UP] = self.layoutsMenu.addAction(self.fourUpIcon, "Four-Up")
    self.layoutsMenuButton.setMenu(self.layoutsMenu)

  def setupCrosshairButton(self):
    self.crosshairButton = self.createButton("", checkable=True, icon=self.crosshairIcon, toolTip="Show crosshair")
    self.crosshairNode = slicer.mrmlScene.GetNthNodeByClass(0, 'vtkMRMLCrosshairNode')

  def setupZFrameViewSettingsGroupBox(self):
    self.zFrameViewSettingsGroupBox = qt.QGroupBox('View options:')
    viewSettingsLayout = qt.QVBoxLayout()
    self.zFrameViewSettingsGroupBox.setLayout(viewSettingsLayout)
    self.showZFrameModelButton = self.createButton("", icon=self.zFrameIcon, checkable=True, toolTip="Display zFrame model")
    self.showTemplateButton = self.createButton("", icon=self.templateIcon, checkable=True, toolTip="Display template")
    self.showNeedlePathButton = self.createButton("", icon=self.needleIcon, checkable=True, toolTip="Display needle path")
    self.showTemplatePathButton = self.createButton("", icon=self.pathIcon, checkable=True, toolTip="Display template paths")

    self.updateViewSettingButtons()
    viewSettingsLayout.addWidget(self.createHLayout([self.layoutsMenuButton, self.showZFrameModelButton, self.showTemplatePathButton]))
    viewSettingsLayout.addWidget(self.createHLayout([self.crosshairButton, self.showTemplateButton, self.showNeedlePathButton]))

  def updateViewSettingButtons(self):
    self.showTemplateButton.enabled = self.logic.templateSuccessfulLoaded
    self.showTemplatePathButton.enabled = self.logic.templateSuccessfulLoaded
    self.showZFrameModelButton.enabled = self.logic.zFrameSuccessfulLoaded
    self.showTemplateButton.checked = False
    self.showTemplatePathButton.checked = False
    self.showZFrameModelButton.checked = False
    self.showNeedlePathButton.checked = False

  def setupSliceWidgets(self):
    self.setupSliceWidget("Red")
    self.setupSliceWidget("Yellow")
    self.setupSliceWidget("Green")
    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)

  def setupSliceWidget(self, name):
    widget = self.layoutManager.sliceWidget(name)
    setattr(self, name.lower()+"Widget", widget)
    setattr(self, name.lower()+"CompositeNode", widget.mrmlSliceCompositeNode())
    setattr(self, name.lower()+"SliceView", widget.sliceView())
    setattr(self, name.lower()+"SliceViewInteractor", widget.sliceView().interactorStyle().GetInteractor())
    logic = widget.sliceLogic()
    setattr(self, name.lower()+"SliceLogic", logic)
    setattr(self, name.lower()+"SliceNode", logic.GetSliceNode())
    setattr(self, name.lower()+"FOV", [])

  def setDefaultOrientation(self):
    self.redSliceNode.SetOrientationToAxial()
    self.yellowSliceNode.SetOrientationToSagittal()
    self.greenSliceNode.SetOrientationToCoronal()

  def setAxialOrientation(self):
    self.redSliceNode.SetOrientationToAxial()
    self.yellowSliceNode.SetOrientationToAxial()
    self.greenSliceNode.SetOrientationToAxial()

  def setupZFrameRegistrationUIElements(self):
    self.zFrameRegistrationGroupBox = qt.QGroupBox()
    self.zFrameRegistrationGroupBoxGroupBoxLayout = qt.QGridLayout()
    self.zFrameRegistrationGroupBox.setLayout(self.zFrameRegistrationGroupBoxGroupBoxLayout)
    self.zFrameRegistrationGroupBox.hide()

    self.applyZFrameRegistrationButton = self.createButton("Run ZFrame Registration", enabled=False)
    self.approveZFrameRegistrationButton = self.createButton("Confirm registration accuracy", enabled=False)
    self.retryZFrameRegistrationButton = self.createButton("Retry", enabled=False)

    self.zFrameRegistrationGroupBoxGroupBoxLayout.addWidget(self.applyZFrameRegistrationButton, 0, 0)
    self.zFrameRegistrationGroupBoxGroupBoxLayout.addWidget(self.createHLayout([self.retryZFrameRegistrationButton,
                                                                                self.approveZFrameRegistrationButton])
                                                            , 1, 0)
    self.zFrameRegistrationGroupBoxGroupBoxLayout.setRowStretch(2,1)
    self.layout.addWidget(self.zFrameRegistrationGroupBox)

  def setupTargetingStepUIElements(self):
    self.targetingGroupBox = qt.QGroupBox()
    self.targetingGroupBoxLayout = qt.QGridLayout()
    self.targetingGroupBox.setLayout(self.targetingGroupBoxLayout)

    self.trackTargetsButton = self.createButton("Track targets", toolTip="Track targets", enabled=False)
    self.skipIntraopSeriesButton = self.createButton("Skip", toolTip="Skip the currently selected series", enabled=False)
    self.caseCompletedButton = self.createButton('Case completed', enabled=False)
    self.setupTargetsTable()
    self.setupIntraopSeriesSelector()

    self.createNewCaseButton = self.createButton("New case")
    self.openCaseButton = self.createDirectoryButton(text="Open case", caption="Open Case", directory=self.caseRootDir)

    self.targetingGroupBoxLayout.addWidget(self.createNewCaseButton, 1, 0)
    self.targetingGroupBoxLayout.addWidget(self.openCaseButton, 1, 1)
    self.targetingGroupBoxLayout.addWidget(self.targetTable, 2, 0, 1, 2)
    self.targetingGroupBoxLayout.addWidget(self.intraopSeriesSelector, 3, 0)
    self.targetingGroupBoxLayout.addWidget(self.skipIntraopSeriesButton, 3, 1)
    self.targetingGroupBoxLayout.addWidget(self.trackTargetsButton, 4, 0, 1, 2)
    self.targetingGroupBoxLayout.addWidget(self.caseCompletedButton, 5, 0, 1, 2)
    self.targetingGroupBoxLayout.setRowStretch(5,1)
    self.layout.addWidget(self.targetingGroupBox)

  def createHelperLabel(self, toolTipText=""):
    helperPixmap = qt.QPixmap(os.path.join(self.iconPath, 'icon-infoBox.png'))
    helperPixmap = helperPixmap.scaled(qt.QSize(23, 20))
    label = self.createLabel("", pixmap=helperPixmap, toolTip=toolTipText)
    label.setCursor(qt.Qt.PointingHandCursor)
    return label

  def setupTargetsTable(self):
    self.targetTable = qt.QTableView()
    self.targetTableModel = CustomTargetTableModel(self.logic)
    self.targetTableModel.setTargetModifiedCallback(self.updateNeedleModel)
    self.targetTable.setModel(self.targetTableModel)
    self.targetTable.setSelectionBehavior(qt.QTableView.SelectRows)
    self.setTargetTableSizeConstraints()
    self.targetTable.verticalHeader().hide()
    self.targetTable.minimumHeight = 150

  def setTargetTableSizeConstraints(self):
    self.targetTable.horizontalHeader().setResizeMode(qt.QHeaderView.Stretch)
    self.targetTable.horizontalHeader().setResizeMode(0, qt.QHeaderView.Fixed)
    self.targetTable.horizontalHeader().setResizeMode(3, qt.QHeaderView.ResizeToContents)
    self.targetTable.horizontalHeader().setResizeMode(4, qt.QHeaderView.ResizeToContents)

  def setupIntraopSeriesSelector(self):
    self.intraopSeriesSelector = qt.QComboBox()
    self.seriesModel = qt.QStandardItemModel()
    self.intraopSeriesSelector.setModel(self.seriesModel)

  def setupSegmentationUIElements(self):
    iconSize = qt.QSize(24, 24)

    self.quickSegmentationButton = self.createButton('Quick Mode', icon=self.quickSegmentationIcon, iconSize=iconSize,
                                                     styleSheet=STYLE.WHITE_BACKGROUND)
    self.applySegmentationButton = self.createButton("", icon=self.greenCheckIcon, iconSize=iconSize,
                                                     styleSheet=STYLE.WHITE_BACKGROUND, enabled=False)
    self.cancelSegmentationButton = self.createButton("", icon=self.cancelSegmentationIcon,
                                                      iconSize=iconSize, enabled=False)
    self.backButton = self.createButton("", icon=self.undoIcon, iconSize=iconSize, enabled=False)
    self.forwardButton = self.createButton("", icon=self.redoIcon, iconSize=iconSize, enabled=False)

    self.applyRegistrationButton = self.createButton("Apply Registration", icon=self.greenCheckIcon, iconSize=iconSize,
                                                     toolTip="Run Registration.")
    self.applyRegistrationButton.setFixedHeight(45)

    self.editorWidgetButton = self.createButton("", icon=self.settingsIcon, toolTip="Show Label Editor",
                                                enabled=False, iconSize=iconSize)

    segmentationButtons = self.createHLayout([self.quickSegmentationButton, self.applySegmentationButton,
                                              self.cancelSegmentationButton, self.backButton, self.forwardButton,
                                              self.editorWidgetButton])
    self.setupEditorWidget()

    self.segmentationGroupBox = qt.QGroupBox()
    self.segmentationGroupBoxLayout = qt.QGridLayout()
    self.segmentationGroupBox.setLayout(self.segmentationGroupBoxLayout)
    self.segmentationGroupBoxLayout.addWidget(segmentationButtons, 0, 0)
    self.segmentationGroupBoxLayout.addWidget(self.editorWidgetParent, 1, 0)
    self.segmentationGroupBoxLayout.addWidget(self.applyRegistrationButton, 2, 0)
    self.segmentationGroupBoxLayout.setRowStretch(3, 1)
    self.segmentationGroupBox.hide()
    self.editorWidgetParent.hide()

  def setupEditorWidget(self):
    self.editorWidgetParent = slicer.qMRMLWidget()
    self.editorWidgetParent.setLayout(qt.QVBoxLayout())
    self.editorWidgetParent.setMRMLScene(slicer.mrmlScene)
    self.editUtil = EditorLib.EditUtil.EditUtil()
    self.editorWidget = EditorWidget(parent=self.editorWidgetParent, showVolumesFrame=False)
    self.editorWidget.setup()
    self.editorParameterNode = self.editUtil.getParameterNode()

  def setupRegistrationWatchBox(self):
    self.registrationGroupBox = qt.QGroupBox()
    self.registrationGroupBoxLayout = qt.QFormLayout()
    self.registrationGroupBox.setLayout(self.registrationGroupBoxLayout)
    self.movingVolumeSelector = self.createComboBox(nodeTypes=["vtkMRMLScalarVolumeNode", ""], showChildNodeTypes=False,
                                                    selectNodeUponCreation=True, toolTip="Pick algorithm input.")
    self.movingLabelSelector = self.createComboBox(nodeTypes=["vtkMRMLLabelMapVolumeNode", ""], showChildNodeTypes=False,
                                                   selectNodeUponCreation=False, toolTip="Pick algorithm input.")
    self.fixedVolumeSelector = self.createComboBox(nodeTypes=["vtkMRMLScalarVolumeNode", ""], noneEnabled=True,
                                                   showChildNodeTypes=False, selectNodeUponCreation=True,
                                                   toolTip="Pick algorithm input.")
    self.fixedLabelSelector = self.createComboBox(nodeTypes=["vtkMRMLLabelMapVolumeNode", ""],
                                                  showChildNodeTypes=False,
                                                  selectNodeUponCreation=True, toolTip="Pick algorithm input.")
    self.fiducialSelector = self.createComboBox(nodeTypes=["vtkMRMLMarkupsFiducialNode", ""], noneEnabled=True,
                                                showChildNodeTypes=False, selectNodeUponCreation=False,
                                                toolTip="Select the Targets")
    self.registrationGroupBoxLayout.addRow("Moving Image Volume: ", self.movingVolumeSelector)
    self.registrationGroupBoxLayout.addRow("Moving Label Volume: ", self.movingLabelSelector)
    self.registrationGroupBoxLayout.addRow("Fixed Image Volume: ", self.fixedVolumeSelector)
    self.registrationGroupBoxLayout.addRow("Fixed Label Volume: ", self.fixedLabelSelector)
    self.registrationGroupBoxLayout.addRow("Targets: ", self.fiducialSelector)
    self.registrationGroupBox.hide()
    self.layout.addWidget(self.registrationGroupBox)

  def setupEvaluationStepUIElements(self):
    self.registrationEvaluationGroupBox = qt.QGroupBox()
    self.registrationEvaluationGroupBoxLayout = qt.QGridLayout()
    self.registrationEvaluationGroupBox.setLayout(self.registrationEvaluationGroupBoxLayout)
    self.registrationEvaluationGroupBox.hide()

    self.setupCollapsibleRegistrationArea()
    self.setupRegistrationValidationButtons()
    self.registrationEvaluationGroupBoxLayout.addWidget(self.segmentationGroupBox, 2, 0)
    self.registrationEvaluationGroupBoxLayout.addWidget(self.collapsibleRegistrationArea, 3, 0)
    self.registrationEvaluationGroupBoxLayout.addWidget(self.evaluationButtonsGroupBox, 5, 0)
    self.registrationEvaluationGroupBoxLayout.setRowStretch(6, 1)
    self.layout.addWidget(self.registrationEvaluationGroupBox)

  def setupRegistrationValidationButtons(self):
    self.approveRegistrationResultButton = self.createButton("Approve", toolTip="Approve")
    self.retryRegistrationButton = self.createButton("Retry", toolTip="Retry")
    self.rejectRegistrationResultButton = self.createButton("Reject", toolTip="Reject")
    self.evaluationButtonsGroupBox = self.createHLayout([self.retryRegistrationButton,
                                                         self.approveRegistrationResultButton, self.rejectRegistrationResultButton])
    self.evaluationButtonsGroupBox.enabled = False

  def setupCollapsibleRegistrationArea(self):
    self.collapsibleRegistrationArea = ctk.ctkCollapsibleButton()
    self.collapsibleRegistrationArea.text = "Registration Results"
    self.registrationGroupBoxDisplayLayout = qt.QFormLayout(self.collapsibleRegistrationArea)

    self.resultSelector = ctk.ctkComboBox()
    self.resultSelector.setFixedWidth(250)
    self.registrationResultAlternatives = self.createHLayout([qt.QLabel('Alternative Registration Result'), self.resultSelector])
    self.registrationGroupBoxDisplayLayout.addWidget(self.registrationResultAlternatives)

    self.showRigidResultButton = self.createButton('Rigid', checkable=True, name='rigid')
    self.showAffineResultButton = self.createButton('Affine', checkable=True, name='affine')
    self.showBSplineResultButton = self.createButton('BSpline', checkable=True, name='bSpline')

    self.registrationButtonGroup = qt.QButtonGroup()
    self.registrationButtonGroup.addButton(self.showRigidResultButton, 1)
    self.registrationButtonGroup.addButton(self.showAffineResultButton, 2)
    self.registrationButtonGroup.addButton(self.showBSplineResultButton, 3)

    self.registrationTypesGroupBox = qt.QGroupBox("Type")
    self.registrationTypesGroupBoxLayout = qt.QFormLayout(self.registrationTypesGroupBox)
    self.registrationTypesGroupBoxLayout.addWidget(self.createVLayout([self.showRigidResultButton,
                                                                       self.showAffineResultButton,
                                                                       self.showBSplineResultButton]))
    self.setupVisualEffectsUIElements()

    self.registrationGroupBoxDisplayLayout.addWidget(self.createHLayout([self.registrationTypesGroupBox,
                                                                         self.visualEffectsGroupBox]))

  def setupVisualEffectsUIElements(self):
    self.opacitySpinBox = qt.QDoubleSpinBox()
    self.opacitySpinBox.minimum = 0
    self.opacitySpinBox.maximum = 1.0
    self.opacitySpinBox.value = 0
    self.opacitySpinBox.singleStep = 0.05

    self.opacitySliderPopup = ctk.ctkPopupWidget(self.opacitySpinBox)
    popupLayout = qt.QHBoxLayout(self.opacitySliderPopup)
    self.opacitySlider = ctk.ctkDoubleSlider(self.opacitySliderPopup)
    self.opacitySlider.orientation = qt.Qt.Horizontal
    self.opacitySlider.minimum = 0
    self.opacitySlider.maximum = 1.0
    self.opacitySlider.value = 0
    self.opacitySlider.singleStep = 0.05

    popupLayout.addWidget(self.opacitySlider)
    self.opacitySliderPopup.verticalDirection = ctk.ctkBasePopupWidget.TopToBottom
    self.opacitySliderPopup.animationEffect = ctk.ctkBasePopupWidget.FadeEffect
    self.opacitySliderPopup.orientation = qt.Qt.Horizontal
    self.opacitySliderPopup.easingCurve = qt.QEasingCurve.OutQuart
    self.opacitySliderPopup.effectDuration = 100

    self.rockCount = 0
    self.rockTimer = qt.QTimer()
    self.rockTimer.setInterval(50)
    self.rockCheckBox = qt.QCheckBox("Rock")
    self.rockCheckBox.checked = False

    self.flickerTimer = qt.QTimer()
    self.flickerTimer.setInterval(400)
    self.flickerCheckBox = qt.QCheckBox("Flicker")
    self.flickerCheckBox.checked = False

    self.animaHolderLayout = self.createHLayout([self.rockCheckBox, self.flickerCheckBox])
    self.visualEffectsGroupBox = qt.QGroupBox("Visual Effects")
    self.visualEffectsGroupBoxLayout = qt.QFormLayout(self.visualEffectsGroupBox)
    self.useRevealCursorButton = self.createButton("", icon=self.revealCursorIcon, checkable=True,
                                                   enabled=False, toolTip="Use reveal cursor")
    slider = self.createHLayout([self.opacitySpinBox, self.animaHolderLayout])
    self.visualEffectsGroupBoxLayout.addWidget(self.createVLayout([slider, self.useRevealCursorButton]))

  def setupConnections(self):

    def setupButtonConnections():
      self.createNewCaseButton.clicked.connect(self.onCreateNewCaseButtonClicked)
      self.openCaseButton.directorySelected.connect(self.onOpenCaseButtonClicked)
      self.casesRootDirectoryButton.directoryChanged.connect(lambda: setattr(self, "caseRootDir",
                                                                              self.casesRootDirectoryButton.directory))
      self.forwardButton.clicked.connect(self.onForwardButtonClicked)
      self.backButton.clicked.connect(self.onBackButtonClicked)
      self.editorWidgetButton.clicked.connect(self.onEditorGearIconClicked)
      self.applyRegistrationButton.clicked.connect(lambda: self.onInvokeRegistration(initial=True))
      self.quickSegmentationButton.clicked.connect(self.onQuickSegmentationButtonClicked)
      self.cancelSegmentationButton.clicked.connect(self.onCancelSegmentationButtonClicked)
      self.trackTargetsButton.clicked.connect(self.onTrackTargetsButtonClicked)
      self.applySegmentationButton.clicked.connect(self.onApplySegmentationButtonClicked)
      self.approveRegistrationResultButton.clicked.connect(self.onApproveRegistrationResultButtonClicked)
      self.skipIntraopSeriesButton.clicked.connect(self.onSkipIntraopSeriesButtonClicked)
      self.rejectRegistrationResultButton.clicked.connect(self.onRejectRegistrationResultButtonClicked)
      self.retryRegistrationButton.clicked.connect(self.onRetryRegistrationButtonClicked)
      self.caseCompletedButton.clicked.connect(self.onCaseCompletedButtonClicked)
      self.registrationDetailsButton.clicked.connect(self.onShowRegistrationDetails)
      self.registrationButtonGroup.connect('buttonClicked(int)', self.onRegistrationButtonChecked)
      self.crosshairButton.clicked.connect(self.onCrosshairButtonClicked)
      self.retryZFrameRegistrationButton.clicked.connect(self.onRetryZFrameRegistrationButtonClicked)
      self.approveZFrameRegistrationButton.clicked.connect(self.onApproveZFrameRegistrationButtonClicked)
      self.applyZFrameRegistrationButton.clicked.connect(self.onApplyZFrameRegistrationButtonClicked)
      self.useRevealCursorButton.connect('toggled(bool)', self.onRevealToggled)
      self.showZFrameModelButton.connect('toggled(bool)', self.onShowZFrameModelToggled)
      self.showTemplateButton.connect('toggled(bool)', self.onShowZFrameTemplateToggled)
      self.showTemplatePathButton.connect('toggled(bool)', self.onShowTemplatePathToggled)
      self.showNeedlePathButton.connect('toggled(bool)', self.onShowNeedlePathToggled)

    def setupSelectorConnections():
      self.resultSelector.connect('currentIndexChanged(QString)', self.onRegistrationResultSelected)
      self.intraopSeriesSelector.connect('currentIndexChanged(QString)', self.onIntraopSeriesSelectionChanged)

    def setupCheckBoxConnections():
      self.rockCheckBox.connect('toggled(bool)', self.onRockToggled)
      self.flickerCheckBox.connect('toggled(bool)', self.onFlickerToggled)

    def setupOtherConnections():
      self.opacitySpinBox.valueChanged.connect(self.onOpacitySpinBoxChanged)
      self.opacitySlider.valueChanged.connect(self.onOpacitySliderChanged)
      self.rockTimer.connect('timeout()', self.onRockToggled)
      self.flickerTimer.connect('timeout()', self.onFlickerToggled)
      self.targetTable.connect('clicked(QModelIndex)', self.onTargetTableSelectionChanged)
      self.layoutsMenu.triggered.connect(self.onLayoutSelectionChanged)
      self.layoutManager.layoutChanged.connect(self.onLayoutChanged)

    setupCheckBoxConnections()
    setupButtonConnections()
    setupSelectorConnections()
    setupOtherConnections()

  def onCreateNewCaseButtonClicked(self):
    self.caseWatchBox.reset()
    if self.currentCaseDirectory is not None:
      self.closeCase()
    self.createNewCaseButton.enabled = False
    self.openCaseButton.enabled = False
    self.currentCaseDirectory = self.logic.createNewCase(self.caseRootDir)
    self.updateCaseWatchbox()
    self.preopDicomReceiver = SmartDICOMReceiver(incomingDataDirectory=self.preopDICOMDataDirectory,
                                                 receiveFinishedCallback=self.onPreopDataReceived)
    self.preopDicomReceiver.start()
    # TODO: stop whenever needs to be stopped

  def onPreopDataReceived(self):
    self.preopDicomReceiver.stop()
    success = self.invokePreProcessing()
    if success:
      self.setSetting('InputLocation', None, moduleName="mpReview")
      slicer.modules.mpreview.widgetRepresentation()
      mpReview = slicer.modules.mpReviewWidget
      self.setSetting('InputLocation', self.mpReviewPreprocessedOutput, moduleName="mpReview")
      mpReview.onReload()
      slicer.modules.mpReviewWidget.saveButton.clicked.connect(self.onReturnFromMpReview)
      self.layoutManager.selectModule(mpReview.moduleName)
    else:
      slicer.util.infoDisplay("No DICOM data could be processed. Please select another directory.",
                              windowTitle="SliceTracker")

  def onReturnFromMpReview(self):
    slicer.modules.mpReviewWidget.saveButton.clicked.disconnect(self.onReturnFromMpReview)
    self.layoutManager.selectModule(self.moduleName)
    self.preopDataDir = self.logic.getFirstStudyFromMpReviewPreprocessed(self.mpReviewPreprocessedOutput)
    self.intraopDataDir = os.path.join(self.currentCaseDirectory, "DICOM", "Intraop")

  def invokePreProcessing(self):
    from mpReviewPreprocessor import mpReviewPreprocessorLogic
    self.mpReviewPreprocessorLogic = mpReviewPreprocessorLogic()
    self.progress = slicer.util.createProgressDialog()
    self.progress.canceled.connect(lambda : self.mpReviewPreprocessorLogic.cancelProcess())
    self.mpReviewPreprocessorLogic.importStudy(self.preopDICOMDataDirectory, progressCallback=self.updateProgressBar)
    success = False
    if self.mpReviewPreprocessorLogic.patientFound():
      success = True
      self.mpReviewPreprocessorLogic.convertData(outputDir=self.mpReviewPreprocessedOutput, copyDICOM=False,
                                                 progressCallback=self.updateProgressBar)
    self.progress.canceled.disconnect(lambda : self.mpReviewPreprocessorLogic.cancelProcess())
    self.progress.close()
    return success

  def onOpenCaseButtonClicked(self):
    self.caseWatchBox.reset()
    self.currentCaseDirectory = self.openCaseButton.directory
    if not self.logic.isCaseDirectoryValid(self.openCaseButton.directory):
      slicer.util.warningDisplay("The selected case directory seems not to be valid", windowTitle="SliceTracker")
      self.closeCase()
      return

    from mpReview import mpReviewLogic

    if self.logic.hasCaseBeenCompleted(self.currentCaseDirectory):
      if not slicer.util.confirmYesNoDisplay("The selected case has already been completed. Would you like to reopen it?"):
        return
        # TODO: reconstruct results from SliceTrackerOutputs if available or continue case
    if mpReviewLogic.wasmpReviewPreprocessed(self.mpReviewPreprocessedOutput):
      self.preopDataDir = self.logic.getFirstStudyFromMpReviewPreprocessed(self.mpReviewPreprocessedOutput)
      self.intraopDataDir = os.path.join(self.currentCaseDirectory, "DICOM", "Intraop")
    self.updateCaseWatchbox()

  def updateCaseWatchbox(self):
    value = self.currentCaseDirectory
    self.caseWatchBox.setInformation("CurrentCaseDirectory", self.truncatePath(value), toolTip=value)
    preop = os.path.join(value, "DICOM", "Preop")
    self.caseWatchBox.setInformation("CurrentPreopDICOMDirectory", self.truncatePath(preop), toolTip=preop)
    intraop = os.path.join(value, "DICOM", "Intraop")
    self.caseWatchBox.setInformation("CurrentIntraopDICOMDirectory", self.truncatePath(intraop), toolTip=intraop)
    mpReviewPreprocessed = os.path.join(value, "mpReviewPreprocessed")
    self.caseWatchBox.setInformation("mpReviewDirectory", self.truncatePath(mpReviewPreprocessed),
                                     toolTip=mpReviewPreprocessed)

  def onShowZFrameModelToggled(self, checked):
    self.logic.setZFrameVisibility(checked)

  def onShowZFrameTemplateToggled(self, checked):
    self.logic.setTemplateVisibility(checked)

  def onShowTemplatePathToggled(self, checked):
    self.logic.setTemplatePathVisibility(checked)

  def onShowNeedlePathToggled(self, checked):
    self.logic.setNeedlePathVisibility(checked)

  def onShowRegistrationDetails(self):
    if self.registrationGroupBox.visible:
      self.registrationGroupBox.hide()
      self.registrationGroupBox.enabled = True
    else:
      self.registrationGroupBox.show()
      self.registrationGroupBox.enabled = False

  def onLayoutChanged(self):
    if self.layoutManager.layout in self.ALLOWED_LAYOUTS:
      self.layoutsMenu.setActiveAction(self.layoutDict[self.layoutManager.layout])
      self.onLayoutSelectionChanged(self.layoutDict[self.layoutManager.layout])
      if self.registrationAssessmentMode:
        self.disableTargetMovingMode()
        self.setupRegistrationResultView()
        self.onRegistrationResultSelected(self.currentResult.name)
    else:
      self.layoutsMenuButton.setIcon(qt.QIcon())
      self.layoutsMenuButton.setText("Layouts")

  def onLayoutSelectionChanged(self, action):
    self.layoutsMenuButton.setIcon(action.icon)
    self.layoutsMenuButton.setText(action.text)
    selectedLayout = self.getLayoutByAction(action)
    if self.layoutManager.layout != selectedLayout:
      self.layoutManager.setLayout(selectedLayout)

  def getLayoutByAction(self, searchedAction):
    for layout, action in self.layoutDict.iteritems():
      if action is searchedAction:
        return layout

  def onCrosshairButtonClicked(self):
    if self.crosshairButton.checked:
      self.crosshairNode.SetCrosshairMode(slicer.vtkMRMLCrosshairNode.ShowSmallBasic)
      self.crosshairNode.SetCrosshairMode(slicer.vtkMRMLCrosshairNode.ShowSmallBasic)
    else:
      self.crosshairNode.SetCrosshairMode(slicer.vtkMRMLCrosshairNode.NoCrosshair)

  def onRegistrationButtonChecked(self, buttonId):
    self.disableTargetMovingMode()
    self.hideAllTargets()
    if buttonId == 1:
      self.onRigidResultClicked()
    elif buttonId == 2:
      if not self.currentResult.affineTargets:
        return self.showBSplineResultButton.click()
      self.onAffineResultClicked()
    elif buttonId == 3:
      self.onBSplineResultClicked()

  def deactivateUndoRedoButtons(self):
    self.forwardButton.setEnabled(0)
    self.backButton.setEnabled(0)

  def updateUndoRedoButtons(self, observer=None, caller=None):
    self.forwardButton.setEnabled(self.deletedMarkups.GetNumberOfFiducials() > 0)
    self.backButton.setEnabled(self.logic.inputMarkupNode.GetNumberOfFiducials() > 0)

  def onIntraopSeriesSelectionChanged(self, selectedSeries=None):
    if self.evaluationMode:
      return
    self.removeSliceAnnotations()
    if selectedSeries:
      trackingPossible = self.isTrackingPossible(selectedSeries)
      self.trackTargetsButton.setEnabled(trackingPossible)
      self.showTemplatePathButton.checked = trackingPossible and self.COVER_PROSTATE in selectedSeries
      self.skipIntraopSeriesButton.setEnabled(trackingPossible)
      self.configureViewersForSelectedIntraopSeries(selectedSeries)
      self.updateIntraopSeriesSelectorColors(selectedSeries)
      self.updateSliceAnnotations(selectedSeries)

  def isTrackingPossible(self, series):
    return self.logic.isTrackingPossible(series) and \
          ((self.GUIDANCE_IMAGE in series and self.registrationResults.getMostRecentApprovedCoverProstateRegistration()) or
          (self.COVER_PROSTATE in series and self.logic.zFrameRegistrationSuccessful) or
          (self.COVER_TEMPLATE in series and not self.logic.zFrameRegistrationSuccessful))

  def updateIntraopSeriesSelectorColors(self, selectedSeries):
    style = STYLE.YELLOW_BACKGROUND
    if not self.isTrackingPossible(selectedSeries):
      if self.registrationResults.registrationResultWasApproved(selectedSeries) or \
              (self.logic.zFrameRegistrationSuccessful and self.COVER_TEMPLATE in selectedSeries):
        style = STYLE.GREEN_BACKGROUND
      elif self.registrationResults.registrationResultWasSkipped(selectedSeries):
        style = STYLE.RED_BACKGROUND
      elif self.registrationResults.registrationResultWasRejected(selectedSeries):
        style = STYLE.GRAY_BACKGROUND
    self.intraopSeriesSelector.setStyleSheet(style)

  def updateSliceAnnotations(self, selectedSeries):
    if not self.isTrackingPossible(selectedSeries):
      annotationText = None
      if self.registrationResults.registrationResultWasApproved(selectedSeries):
        annotationText = self.APPROVED_RESULT_TEXT_ANNOTATION
      elif self.registrationResults.registrationResultWasRejected(selectedSeries):
        annotationText = self.REJECTED_RESULT_TEXT_ANNOTATION
      if annotationText:
        self.sliceAnnotations.append(SliceAnnotation(self.yellowWidget, annotationText, fontSize=15, yPos=20))
      if self.registrationResults.registrationResultWasSkipped(selectedSeries):
        self.sliceAnnotations.append(SliceAnnotation(self.redWidget, self.SKIPPED_RESULT_TEXT_ANNOTATION,
                                                       fontSize=15, yPos=20))

  def configureViewersForSelectedIntraopSeries(self, selectedSeries):
    if self.registrationResults.registrationResultWasApproved(selectedSeries) or \
            self.registrationResults.registrationResultWasRejected(selectedSeries):
      self.setupSideBySideRegistrationView()
    else:
      try:
        result = self.registrationResults.getResultsBySeries(selectedSeries)[0]
      except IndexError:
        volume, _ = self.logic.getOrCreateVolumeForSeries(selectedSeries)
        self.setupRedSlicePreview(volume)
        return
      self.setupRedSlicePreview(result.fixedVolume)

  def resetVisualEffects(self):
    self.flickerCheckBox.checked = False
    self.rockCheckBox.checked = False
    self.useRevealCursorButton.enabled = False
    self.useRevealCursorButton.checked = False

  def setupFourUpView(self, volume):
    self.disableTargetTable()
    self.setBackgroundToVolume(volume.GetID())
    self.layoutManager.setLayout(self.LAYOUT_FOUR_UP)
    slicer.app.applicationLogic().FitSliceToAll()

  def setupRedSlicePreview(self, volume):
    self.disableTargetTable()
    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)
    self.setBackgroundToVolume(volume.GetID())
    slicer.app.applicationLogic().FitSliceToAll()

  def setupSideBySideRegistrationView(self):
    # TODO: shall we add a selector for viewing different registration results that has been rejected?
    self.targetTable.enabled = True
    for result in self.registrationResults.getResultsBySeries(self.intraopSeriesSelector.currentText):
      if result.approved or result.rejected:
        self.layoutManager.setLayout(self.LAYOUT_SIDE_BY_SIDE)
        self.setupRegistrationResultSideBySideView()
        if result.rejected:
          self.onRegistrationResultSelected(result.name, registrationType='bSpline')
        elif result.approved:
          self.onRegistrationResultSelected(result.name, registrationType=result.approvedRegistrationType)
        break

  def onTargetTableSelectionChanged(self, modelIndex=None):
    if not modelIndex:
      self.getAndSelectTargetFromTable()
      return
    if self.moveTargetMode is True and modelIndex != self.currentlyMovedTargetModelIndex:
      self.disableTargetMovingMode()
    self.lastSelectedModelIndex = modelIndex
    row = modelIndex.row()
    if not self.currentTargets:
      self.currentTargets = self.logic.preopTargets
    self.jumpSliceNodesToNthTarget(row)
    self.updateNeedleModel()

  def jumpSliceNodesToNthTarget(self, targetIndex):
    currentTargetsSliceNodes = [self.yellowSliceNode]
    if self.layoutManager.layout == self.LAYOUT_SIDE_BY_SIDE:
      self.jumpSliceNodeToTarget(self.redSliceNode, self.logic.preopTargets, targetIndex)
      self.setTargetSelected(self.logic.preopTargets, selected=False)
      self.logic.preopTargets.SetNthFiducialSelected(targetIndex, True)
    else:
      currentTargetsSliceNodes = [self.redSliceNode, self.yellowSliceNode, self.greenSliceNode]
    for sliceNode in currentTargetsSliceNodes:
      self.jumpSliceNodeToTarget(sliceNode, self.currentTargets, targetIndex)
    self.setTargetSelected(self.currentTargets, selected=False)
    self.currentTargets.SetNthFiducialSelected(targetIndex, True)

  def onMoveTargetRequest(self, modelIndex):
    if self.moveTargetMode:
      self.disableTargetMovingMode()
      if self.currentlyMovedTargetModelIndex != modelIndex:
        self.onMoveTargetRequest(modelIndex)
      self.currentlyMovedTargetModelIndex = None
    else:
      self.currentlyMovedTargetModelIndex = modelIndex
      self.enableTargetMovingMode()

  def enableTargetMovingMode(self):
    self.clearTargetMovementObserverAndAnnotations()
    targetName = self.targetTableModel.targetList.GetNthFiducialLabel(self.currentlyMovedTargetModelIndex.row())

    widgets = [self.yellowWidget] if self.layoutManager.layout == self.LAYOUT_SIDE_BY_SIDE else \
                 [self.redWidget, self.yellowWidget, self.greenWidget]
    for widget in widgets:
      sliceView = widget.sliceView()
      interactor = sliceView.interactorStyle().GetInteractor()
      observer = interactor.AddObserver(vtk.vtkCommand.LeftButtonReleaseEvent, self.onViewerClickEvent)
      sliceView.setCursor(qt.Qt.CrossCursor)
      annotation = SliceAnnotation(widget, "Target Movement Mode (%s)" % targetName, opacity=0.5,
                                   verticalAlign="top", horizontalAlign="center")
      self.mouseReleaseEventObservers[widget] = (observer, annotation)
    self.moveTargetMode = True

  def disableTargetMovingMode(self):
    self.clearTargetMovementObserverAndAnnotations()
    self.mouseReleaseEventObservers = {}
    self.moveTargetMode = False

  def clearTargetMovementObserverAndAnnotations(self):
    for widget, (observer, annotation) in self.mouseReleaseEventObservers.iteritems():
      sliceView = widget.sliceView()
      interactor = sliceView.interactorStyle().GetInteractor()
      interactor.RemoveObserver(observer)
      sliceView.setCursor(qt.Qt.ArrowCursor)
      annotation.remove()

  def onViewerClickEvent(self, observee=None, event=None):
    posXY = observee.GetEventPosition()
    widget = self.getWidgetForInteractor(observee)
    posRAS = self.xyToRAS(widget.sliceLogic(), posXY)
    if self.currentlyMovedTargetModelIndex is not None:
      self.currentResult.isGoingToBeMoved(self.targetTableModel.targetList, self.currentlyMovedTargetModelIndex.row())
      self.targetTableModel.targetList.SetNthFiducialPositionFromArray(self.currentlyMovedTargetModelIndex.row(), posRAS)
    self.disableTargetMovingMode()

  def getWidgetForInteractor(self, observee):
    for widget in self.mouseReleaseEventObservers.keys():
      sliceView = widget.sliceView()
      interactor = sliceView.interactorStyle().GetInteractor()
      if interactor is observee:
        return widget
    return None

  def xyToRAS(self, sliceLogic, xyPoint):
    sliceNode = sliceLogic.GetSliceNode()
    rast = sliceNode.GetXYToRAS().MultiplyPoint(xyPoint + (0,1,))
    return rast[:3]

  def updateNeedleModel(self):
    if self.showNeedlePathButton.checked and self.logic.zFrameRegistrationSuccessful:
      modelIndex = self.lastSelectedModelIndex
      try:
        start, end = self.targetTableModel.needleStartEndPositions[modelIndex.row()]
        self.logic.createNeedleModelNode(start, end)
      except KeyError:
        self.logic.removeNeedleModelNode()

  def getAndSelectTargetFromTable(self):
    modelIndex = None
    if self.lastSelectedModelIndex:
      modelIndex = self.lastSelectedModelIndex
    else:
      if self.targetTableModel.rowCount():
        modelIndex = self.targetTableModel.index(0,0)
    if modelIndex:
      self.targetTable.clicked(modelIndex)

  def jumpSliceNodeToTarget(self, sliceNode, targetNode, n):
    point = [0,0,0,0]
    targetNode.GetMarkupPointWorld(n, 0, point)
    sliceNode.JumpSlice(point[0], point[1], point[2])

  def updateRegistrationResultSelector(self):
    self.resultSelector.clear()
    results = self.registrationResults.getResultsBySeriesNumber(self.currentResult.seriesNumber)
    for result in reversed(results):
      self.resultSelector.addItem(result.name)
    self.registrationResultAlternatives.visible = len(results) > 1

  def removeSliceAnnotations(self):
    for annotation in self.sliceAnnotations:
      annotation.remove()
    self.sliceAnnotations = []
    self.removeZFrameInstructionAnnotation()

  def addSideBySideSliceAnnotations(self):
    self.removeSliceAnnotations()
    self.sliceAnnotations.append(SliceAnnotation(self.redWidget, self.LEFT_VIEWER_SLICE_ANNOTATION_TEXT, fontSize=30,
                                                 yPos=55))
    self.sliceAnnotations.append(SliceAnnotation(self.yellowWidget, self.RIGHT_VIEWER_SLICE_ANNOTATION_TEXT, yPos=55,
                                                 fontSize=30))
    self.registrationResultNewImageAnnotation = SliceAnnotation(self.yellowWidget,
                                                                self.RIGHT_VIEWER_SLICE_NEEDLE_IMAGE_ANNOTATION_TEXT, yPos=35,
                                                                opacity=0.0, color=(0,0.5,0))
    self.sliceAnnotations.append(self.registrationResultNewImageAnnotation)
    self.registrationResultOldImageAnnotation = SliceAnnotation(self.yellowWidget,
                                                                self.RIGHT_VIEWER_SLICE_TRANSFORMED_ANNOTATION_TEXT, yPos=35)
    self.sliceAnnotations.append(self.registrationResultOldImageAnnotation)
    self.registrationResultStatusAnnotation = None

  def addFourUpSliceAnnotations(self):
    self.removeSliceAnnotations()
    for widget in (self.redWidget, self.yellowWidget, self.greenWidget):
      self.sliceAnnotations.append(SliceAnnotation(widget, self.RIGHT_VIEWER_SLICE_ANNOTATION_TEXT, yPos=55, fontSize=30))
    self.registrationResultNewImageAnnotation = SliceAnnotation(self.redWidget,
                                                                self.RIGHT_VIEWER_SLICE_NEEDLE_IMAGE_ANNOTATION_TEXT, yPos=35,
                                                                opacity=0.0, color=(0,0.5,0))
    self.sliceAnnotations.append(self.registrationResultNewImageAnnotation)
    self.registrationResultOldImageAnnotation = SliceAnnotation(self.redWidget,
                                                                self.RIGHT_VIEWER_SLICE_TRANSFORMED_ANNOTATION_TEXT, yPos=35)
    self.sliceAnnotations.append(self.registrationResultOldImageAnnotation)
    self.registrationResultStatusAnnotation = None

  def onForwardButtonClicked(self):
    numberOfDeletedTargets = self.deletedMarkups.GetNumberOfFiducials()
    logging.debug(('numberOfTargets in deletedMarkups is' + str(numberOfDeletedTargets)))
    pos = [0.0, 0.0, 0.0]

    if numberOfDeletedTargets > 0:
      self.deletedMarkups.GetNthFiducialPosition(numberOfDeletedTargets - 1, pos)

    logging.debug(('deletedMarkups.position = ' + str(pos)))

    if pos == [0.0, 0.0, 0.0]:
      logging.debug('pos was 0,0,0 -> go on')
    else:
      self.logic.inputMarkupNode.AddFiducialFromArray(pos)
      self.deletedMarkups.RemoveMarkup(numberOfDeletedTargets - 1)

    self.updateUndoRedoButtons()

  def onBackButtonClicked(self):
    activeFiducials = self.logic.inputMarkupNode
    numberOfTargets = activeFiducials.GetNumberOfFiducials()
    logging.debug('numberOfTargets is' + str(numberOfTargets))
    pos = [0.0, 0.0, 0.0]
    activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
    logging.debug('activeFiducials.position = ' + str(pos))

    if numberOfTargets > 0:
      self.deletedMarkups.GetNthFiducialPosition(numberOfTargets - 1, pos)

    activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
    logging.debug('POS BEFORE ENTRY = ' + str(pos))
    if pos == [0.0, 0.0, 0.0]:
      logging.debug('pos was 0,0,0 -> go on')
    else:
      # add it to deletedMarkups
      activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
      # logging.debug(('pos = '+str(pos))
      self.deletedMarkups.AddFiducialFromArray(pos)
      logging.debug('added Markup with position ' + str(pos) + ' to the deletedMarkupsList')
      # delete it in activeFiducials
      activeFiducials.RemoveMarkup(numberOfTargets - 1)

    self.updateUndoRedoButtons()

  def onRevealToggled(self, checked):
    if self.revealCursor:
      self.revealCursor.tearDown()
    if checked:
      import CompareVolumes
      self.revealCursor = CompareVolumes.LayerReveal()

  def setOldNewIndicatorAnnotationOpacity(self, value):
    self.registrationResultNewImageAnnotation.opacity = value
    self.registrationResultOldImageAnnotation.opacity = 1.0 - value

  def showOpacitySliderPopup(self, show):
    if show:
      if not self.opacitySliderPopup.visible:
        self.opacitySpinBox.enabled = False
        self.opacitySlider.enabled = False
        self.opacitySliderPopup.show()
        self.opacitySliderPopup.autoHide = False
    else:
      self.opacitySpinBox.enabled = True
      self.opacitySlider.enabled = True
      self.opacitySliderPopup.hide()
      self.opacitySliderPopup. autoHide = True

  def onRockToggled(self):

    def startRocking():
      self.showOpacitySliderPopup(True)
      self.flickerCheckBox.enabled = False
      self.rockTimer.start()
      self.opacitySpinBox.value = 0.5 + numpy.sin(self.rockCount / 10.) / 2.
      self.rockCount += 1

    def stopRocking():
      self.showOpacitySliderPopup(False)
      self.flickerCheckBox.enabled  = True
      self.rockTimer.stop()

    if self.rockCheckBox.checked:
      startRocking()
    else:
      stopRocking()

  def onFlickerToggled(self):

    def startFlickering():
      self.showOpacitySliderPopup(True)
      self.rockCheckBox.setEnabled(False)
      self.flickerTimer.start()
      self.opacitySpinBox.value = 1.0 if self.opacitySpinBox.value == 0.0 else 0.0

    def stopFlickering():
      self.showOpacitySliderPopup(False)
      self.rockCheckBox.setEnabled(True)
      self.flickerTimer.stop()
      self.opacitySpinBox.value = 0.0

    if self.flickerCheckBox.checked:
      startFlickering()
    else:
      stopFlickering()

  def closeCase(self):
    # TODO: think about what else to release
    self.logic.closeCase(self.currentCaseDirectory)
    self.caseCompletedButton.enabled = False
    self.patientWatchBox.sourceFile = None
    self.intraopWatchBox.sourceFile = None
    self.caseWatchBox.reset()
    self.logic.stopWatching()
    self.logic.stopStoreSCP()
    self.createNewCaseButton.enabled = True
    self.openCaseButton.enabled = True
    self.currentCaseDirectory = None

  def onCaseCompletedButtonClicked(self):
    self.save(showDialog=True)
    self.logic.completeCase(self.currentCaseDirectory)
    self.closeCase()

  def save(self, showDialog=False):
    if not os.path.exists(self.outputDir) or self.generatedOutputDirectory == "":
      slicer.util.infoDisplay("CRITICAL ERROR: You need to provide a valid output directory for saving data. Please make "
                              "sure to select one.", windowTitle="SliceTracker")
    else:
      message = self.logic.save(self.generatedOutputDirectory)
      if showDialog:
        slicer.util.infoDisplay(message, windowTitle="SliceTracker")

  def configureSegmentationMode(self):
    if self.COVER_PROSTATE in self.intraopSeriesSelector.currentText:
      self.showTemplatePathButton.checked = False
    self.fixedVolumeSelector.setCurrentNode(self.logic.currentIntraopVolume)
    self.applyRegistrationButton.setEnabled(False)
    self.quickSegmentationButton.setEnabled(self.logic.currentIntraopVolume is not None)
    self.setupFourUpView(self.logic.currentIntraopVolume)
    self.onQuickSegmentationButtonClicked()

  def inputsAreSet(self):
    return not (self.movingVolumeSelector.currentNode() is None and self.fixedVolumeSelector.currentNode() is None and
                self.movingLabelSelector.currentNode() is None and self.fixedLabelSelector.currentNode() is None and
                self.fiducialSelector.currentNode() is None)

  def updateIntraopSeriesSelectorTable(self):
    self.intraopSeriesSelector.blockSignals(True)
    seriesList = self.logic.seriesList
    for series in seriesList:
      sItem = self.getOrCreateItem(series)
      color = COLOR.YELLOW
      if self.registrationResults.registrationResultWasApproved(series) or \
        (self.COVER_TEMPLATE in series and self.logic.zFrameRegistrationSuccessful):
        color = COLOR.GREEN
      elif self.registrationResults.registrationResultWasSkipped(series):
        color = COLOR.RED
      elif self.registrationResults.registrationResultWasRejected(series):
        color = COLOR.GRAY
      self.seriesModel.setData(sItem.index(), color, qt.Qt.BackgroundRole)
    self.intraopSeriesSelector.setCurrentIndex(-1)
    self.intraopSeriesSelector.blockSignals(False)
    self.selectMostRecentEligibleSeries()

  def getOrCreateItem(self, series):
    index = self.intraopSeriesSelector.findText(series)
    if index != -1:
      sItem = self.seriesModel.item(index)
    else:
      sItem = qt.QStandardItem(series)
      self.seriesModel.appendRow(sItem)
    return sItem

  def selectMostRecentEligibleSeries(self):
    if self.evaluationMode:
      self.intraopSeriesSelector.blockSignals(True)
    substring = self.GUIDANCE_IMAGE
    index = -1
    if not self.registrationResults.getMostRecentApprovedCoverProstateRegistration():
      substring = self.COVER_TEMPLATE if not self.logic.zFrameRegistrationSuccessful else self.COVER_PROSTATE
    for item in list(reversed(range(len(self.logic.seriesList)))):
      series = self.seriesModel.item(item).text()
      if substring in series:
        index = self.intraopSeriesSelector.findText(series)
        break
    if index != -1:
      self.intraopSeriesSelector.setCurrentIndex(index)
    self.intraopSeriesSelector.blockSignals(False)

  def onRegistrationResultSelected(self, seriesText, registrationType=None):
    self.disableTargetMovingMode()
    if not seriesText:
      return
    self.hideAllTargets()
    self.currentResult = seriesText
    self.showAffineResultButton.setEnabled(self.GUIDANCE_IMAGE not in seriesText)
    if registrationType:
      for button in self.registrationButtonGroup.buttons():
        if button.name == registrationType:
          button.click()
          break
    elif self.registrationButtonGroup.checkedId() != -1:
      self.onRegistrationButtonChecked(self.registrationButtonGroup.checkedId())
    else:
      self.showBSplineResultButton.click()

  def hideAllTargets(self):
    for result in self.registrationResults.getResultsAsList():
      for targetNode in [targets for targets in result.targets.values() if targets]:
        self.setTargetVisibility(targetNode, show=False)
    self.setTargetVisibility(self.logic.preopTargets, show=False)

  def onRigidResultClicked(self):
    self.targetTableModel.targetList = self.currentResult.rigidTargets
    self.displayRegistrationResults(registrationType='rigid')

  def onAffineResultClicked(self):
    self.targetTableModel.targetList = self.currentResult.affineTargets
    self.displayRegistrationResults(registrationType='affine')

  def onBSplineResultClicked(self):
    self.targetTableModel.targetList = self.currentResult.bSplineTargets
    self.displayRegistrationResults(registrationType='bSpline')

  def displayRegistrationResults(self, registrationType):
    self.setCurrentRegistrationResultSliceViews(registrationType)
    self.showTargets(registrationType=registrationType)
    self.visualEffectsGroupBox.setEnabled(True)
    if self.lastSelectedModelIndex:
      self.targetTable.clicked(self.lastSelectedModelIndex)

  def setDefaultFOV(self, sliceLogic, volume, factor=0.5):
    sliceLogic.FitSliceToAll()
    FOV = sliceLogic.GetSliceNode().GetFieldOfView()
    self.setFOV(sliceLogic, [FOV[0] * factor, FOV[1] * factor, FOV[2]])
    sliceNode = sliceLogic.GetSliceNode()
    sliceNode.RotateToVolumePlane(volume)

  def setFOV(self, sliceLogic, FOV):
    sliceNode = sliceLogic.GetSliceNode()
    sliceNode.SetFieldOfView(FOV[0], FOV[1], FOV[2])
    sliceNode.UpdateMatrices()

  def setCurrentRegistrationResultSliceViews(self, registrationType):
    compositeNodes = [self.yellowCompositeNode]
    if self.layoutManager.layout == self.LAYOUT_SIDE_BY_SIDE:
      self.redCompositeNode.SetForegroundVolumeID(None)
      self.redCompositeNode.SetBackgroundVolumeID(self.logic.preopVolume.GetID())
    else:
      compositeNodes = [self.redCompositeNode, self.yellowCompositeNode, self.greenCompositeNode]

    bgVolume = self.currentResult.getVolume(registrationType)
    bgVolume = bgVolume if self.logic.isVolumeExtentValid(bgVolume) else self.currentResult.fixedVolume

    for compositeNode in compositeNodes:
      compositeNode.SetForegroundVolumeID(self.currentResult.fixedVolume.GetID())
      compositeNode.SetBackgroundVolumeID(bgVolume.GetID())

    self.setDefaultFOV(self.redSliceLogic, self.logic.preopVolume if self.layoutManager.layout == self.LAYOUT_SIDE_BY_SIDE
                                                            else bgVolume)
    self.setDefaultFOV(self.yellowSliceLogic, self.currentResult.getVolume(registrationType))
    self.setDefaultFOV(self.greenSliceLogic, self.currentResult.getVolume(registrationType))

  def showTargets(self, registrationType):
    self.setTargetVisibility(self.currentResult.rigidTargets, show=registrationType == 'rigid')
    self.setTargetVisibility(self.currentResult.bSplineTargets, show=registrationType == 'bSpline')
    if self.currentResult.affineTargets:
      self.setTargetVisibility(self.currentResult.affineTargets, show=registrationType == 'affine')
    self.currentTargets = getattr(self.currentResult, registrationType+'Targets')
    self.setTargetVisibility(self.logic.preopTargets)

  def setTargetVisibility(self, targetNode, show=True):
    self.markupsLogic.SetAllMarkupsVisibility(targetNode, show)

  def setTargetSelected(self, targetNode, selected=False):
    self.markupsLogic.SetAllMarkupsSelected(targetNode, selected)

  def loadPreopData(self):
    dicomFileName = self.logic.getFileList(self.preopDICOMDataDirectory)[0]
    self.patientWatchBox.sourceFile = os.path.join(self.preopDICOMDataDirectory, dicomFileName)
    self.currentID = self.patientWatchBox.getInformation("PatientID")
    message = self.loadMpReviewProcessedData()
    if message:
      slicer.util.warningDisplay(message, winowTitle="SliceTracker")
      return

    success = self.logic.loadT2Label() and self.logic.loadPreopVolume() and self.logic.loadPreopTargets()
    if not success:
      slicer.util.warningDisplay("Loading preop data failed.\nMake sure that the correct directory structure like mpReview "
                                 "explains is used. SliceTracker expects a volume, label and target")
      return

    #TODO: set intraop here

    self.movingLabelSelector.setCurrentNode(self.logic.preopLabel)
    self.logic.preopLabel.GetDisplayNode().SetAndObserveColorNodeID('vtkMRMLColorTableNode1')

    self.configureRedSliceNodeForPreopData()
    self.promptUserAndApplyBiasCorrectionIfNeeded()

    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)
    self.setDefaultFOV(self.redSliceLogic, self.logic.preopVolume)
    self.setupPreopLoadedTargets()

  def loadMpReviewProcessedData(self):
    from mpReview import mpReviewLogic
    resourcesDir = os.path.join(self.preopDataDir, 'RESOURCES')\

    if not os.path.exists(resourcesDir):
      message = "The selected directory does not fit the mpReview directory structure. Make sure that you select the " \
                "study root directory which includes directories RESOURCES"
      return message

    self.progress = slicer.util.createProgressDialog(maxiumu=len(os.listdir(resourcesDir)))
    seriesMap, metaFile = mpReviewLogic.loadMpReviewProcessedData(resourcesDir,
                                                                  updateProgressCallback=self.updateProgressBar)
    self.progress.delete()

    # TODO: targets shall be reference image specific in mpReview
    self.logic.preopTargetsPath = os.path.join(self.preopDataDir, 'Targets')

    self.logic.loadPreopImageAndSegmentation(seriesMap)

    if self.logic.preopSegmentationPath is None:
      message = "No segmentations found.\nMake sure that you used mpReview for segmenting the prostate first and using " \
                "its output as the preop data input here."
      return message
    return None

  def configureRedSliceNodeForPreopData(self):
    self.redSliceNode.RotateToVolumePlane(self.logic.preopLabel)
    self.redSliceNode.SetUseLabelOutline(True)
    self.redSliceNode.SetOrientationToAxial()
    self.redCompositeNode.SetLabelOpacity(1)

  def setupPreopLoadedTargets(self):
    self.setTargetVisibility(self.logic.preopTargets, show=True)
    self.targetTableModel.targetList = self.logic.preopTargets
    self.fiducialSelector.setCurrentNode(self.logic.preopTargets)
    self.logic.preopTargets.SetAndObserveDisplayNodeID(self.logic.preopTargetDisplayNode.GetID())
    self.markupsLogic.JumpSlicesToNthPointInMarkup(self.logic.preopTargets.GetID(), 0)
    self.targetTable.selectRow(0)

  def promptUserAndApplyBiasCorrectionIfNeeded(self):
    if slicer.util.confirmYesNoDisplay("Was an endorectal coil used for preop image acquisition?",
                                       windowTitle="SliceTracker"):
      progress = slicer.util.createProgressDialog(maximum=2, value=1)
      progress.labelText = '\nBias Correction'
      self.logic.applyBiasCorrection()
      progress.setValue(2)
      progress.close()
    self.movingVolumeSelector.setCurrentNode(self.logic.preopVolume)

  def checkForPatientIdSimilarityAndGetSeriesNumbers(self, fileList):
    newSeries = {}
    acceptedSeriesNumbers = []
    for currentFile in fileList:
      currentFile = os.path.join(self.intraopDataDir, currentFile)
      seriesNumber = int(self.logic.getDICOMValue(currentFile, DICOMTAGS.SERIES_NUMBER))
      if seriesNumber not in newSeries.keys():
        patientID = self.logic.getDICOMValue(currentFile, DICOMTAGS.PATIENT_ID)
        if patientID is not None and patientID != self.currentID:
          if not slicer.util.confirmYesNoDisplay(message='WARNING: Preop data of Patient ID ' + self.currentID + ' was selected, but '
                                          ' data of patient with ID ' + patientID + ' just arrived in the folder, which '
                                          'you selected for incoming data.\nDo you want to keep this series?',
                                                 title="PatientsID Not Matching", windowTitle="SliceTracker"):
            self.logic.deleteSeriesFromSeriesList(seriesNumber)
            continue
        acceptedSeriesNumbers.append(seriesNumber)
        newSeries[seriesNumber] = patientID

    acceptedSeriesNumbers.sort()
    return acceptedSeriesNumbers

  def onCancelSegmentationButtonClicked(self):
    if slicer.util.confirmYesNoDisplay("Do you really want to cancel the segmentation process?",
                                       windowTitle="SliceTracker"):
      self.setQuickSegmentationModeOFF()

  def onQuickSegmentationButtonClicked(self):
    self.hideAllLabels()
    self.setBackgroundToVolume(self.logic.currentIntraopVolume.GetID())
    self.setQuickSegmentationModeON()

  def setBackgroundToVolume(self, volumeID):
    for compositeNode in [self.redCompositeNode, self.yellowCompositeNode, self.greenCompositeNode]:
      compositeNode.Reset(None)
      compositeNode.SetBackgroundVolumeID(volumeID)
    self.setDefaultOrientation()
    slicer.app.applicationLogic().FitSliceToAll()

  def hideAllLabels(self):
    for compositeNode in [self.redCompositeNode, self.yellowCompositeNode, self.greenCompositeNode]:
      compositeNode.SetLabelVolumeID(None)

  def setQuickSegmentationModeON(self):
    self.logic.deleteClippingData()
    self.setSegmentationButtons(segmentationActive=True)
    self.deactivateUndoRedoButtons()
    self.disableEditorWidgetAndResetEditorTool()
    self.setupQuickModeHistory()
    self.layoutManager.setLayout(self.LAYOUT_FOUR_UP)
    self.logic.runQuickSegmentationMode()
    # TODO: remove Observer after segmentation finished
    self.logic.inputMarkupNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.updateUndoRedoButtons)

  def disableEditorWidgetAndResetEditorTool(self, enabledButton=False):
    self.editorWidgetParent.hide()
    self.editorParameterNode.SetParameter('effect', 'DefaultTool')
    self.editorWidgetButton.setEnabled(enabledButton)

  def setQuickSegmentationModeOFF(self):
    self.setSegmentationButtons(segmentationActive=False)
    self.deactivateUndoRedoButtons()
    self.resetToRegularViewMode()

  def setSegmentationButtons(self, segmentationActive=False):
    self.quickSegmentationButton.setEnabled(not segmentationActive)
    self.applySegmentationButton.setEnabled(segmentationActive)
    self.cancelSegmentationButton.setEnabled(segmentationActive)

  def setupQuickModeHistory(self):
    try:
      self.deletedMarkups.Reset(None)
    except AttributeError:
      self.deletedMarkups = slicer.vtkMRMLMarkupsFiducialNode()
      self.deletedMarkups.SetName('deletedMarkups')

  def resetToRegularViewMode(self):
    interactionNode = slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton")
    interactionNode.SwitchToViewTransformMode()
    interactionNode.SetPlaceModePersistence(0)

  def onOpacitySpinBoxChanged(self, value):
    if self.opacitySlider.value != value:
      self.opacitySlider.value = value
    self.onOpacityChanged(value)

  def onOpacitySliderChanged(self, value):
    if self.opacitySpinBox.value != value:
      self.opacitySpinBox.value = value

  def onOpacityChanged(self, value):
    #TODO: Seems like having a delay here in all views.
    if self.layoutManager.layout == self.LAYOUT_FOUR_UP:
      self.redCompositeNode.SetForegroundOpacity(value)
      self.greenCompositeNode.SetForegroundOpacity(value)
    self.yellowCompositeNode.SetForegroundOpacity(value)
    self.setOldNewIndicatorAnnotationOpacity(value)

  def activateEvaluationStep(self):
    self.evaluationMode = True
    self.targetingGroupBox.hide()
    self.registrationEvaluationGroupBoxLayout.addWidget(self.targetTable, 4, 0)
    self.registrationEvaluationGroupBox.show()
    self.useRevealCursorButton.enabled = True

  def connectCrosshairNode(self):
    if not self.crosshairNodeObserverTag:
      self.crosshairNodeObserverTag = self.crosshairNode.AddObserver(slicer.vtkMRMLCrosshairNode.CursorPositionModifiedEvent,
                                                                     self.calcCursorTargetsDistance)

  def disconnectCrosshairNode(self):
    if self.crosshairNode and self.crosshairNodeObserverTag:
      self.crosshairNode.RemoveObserver(self.crosshairNodeObserverTag)
    self.crosshairNodeObserverTag = None

  def calcCursorTargetsDistance(self, observee, event):
    ras = [0.0,0.0,0.0]
    xyz = [0.0,0.0,0.0]
    insideView = self.crosshairNode.GetCursorPositionRAS(ras)
    sliceNode = self.crosshairNode.GetCursorPositionXYZ(xyz)

    if not insideView or sliceNode not in [self.redSliceNode, self.yellowSliceNode, self.greenSliceNode]:
      self.targetTableModel.cursorPosition = None
      return

    if (self.registrationAssessmentMode and (self.layoutManager.layout == self.LAYOUT_FOUR_UP or
       (self.layoutManager.layout == self.LAYOUT_SIDE_BY_SIDE and sliceNode is self.yellowSliceNode))) or \
      (not self.registrationAssessmentMode and sliceNode is self.yellowSliceNode):
      self.targetTableModel.cursorPosition = ras

  def openTargetingStep(self, ratingResult=None):
    self.zFrameRegistrationGroupBox.hide()
    self.logic.removeNeedleModelNode()
    self.targetTableModel.computeCursorDistances = False
    self.evaluationMode = False
    self.save()
    self.disconnectCrosshairNode()
    self.hideAllLabels()
    if ratingResult:
      self.currentResult.score = ratingResult
    self.updateIntraopSeriesSelectorTable()
    self.registrationEvaluationGroupBox.hide()
    self.targetingGroupBoxLayout.addWidget(self.targetTable, 2, 0, 1, 2)
    self.targetingGroupBox.show()
    self.removeSliceAnnotations()
    self.resetVisualEffects()

  def onRetryRegistrationButtonClicked(self):
    self.registrationAssessmentMode = False
    self.logic.retryMode = True
    self.evaluationButtonsGroupBox.enabled = False
    self.onTrackTargetsButtonClicked()

  def onApproveRegistrationResultButtonClicked(self):
    self.currentResult.approve(registrationType=self.registrationButtonGroup.checkedButton().name)

    if self.ratingWindow.isRatingEnabled():
      self.ratingWindow.show(disableWidget=self.parent, callback=self.openTargetingStep)
    else:
      self.openTargetingStep()

  def onRejectRegistrationResultButtonClicked(self):
    results = self.registrationResults.getResultsBySeriesNumber(self.currentResult.seriesNumber)
    for result in results:
      result.reject()
    self.openTargetingStep()

  def onSkipIntraopSeriesButtonClicked(self):
    self.skipSeries(self.intraopSeriesSelector.currentText)
    self.openTargetingStep()

  def skipAllUnregisteredPreviousSeries(self, selectedSeries):
    selectedSeriesNumber = RegistrationResult.getSeriesNumberFromString(selectedSeries)
    for series in self.logic.seriesList:
      currentSeriesNumber = RegistrationResult.getSeriesNumberFromString(series)
      if currentSeriesNumber < selectedSeriesNumber:
        results = self.registrationResults.getResultsBySeriesNumber(currentSeriesNumber)
        if len(results) == 0:
          self.skipSeries(series)
      else:
        break

  def skipSeries(self, seriesText):
    volume, _ = self.logic.getOrCreateVolumeForSeries(seriesText)
    name, suffix = self.logic.getRegistrationResultNameAndGeneratedSuffix(volume.GetName())
    result = self.registrationResults.createResult(name+suffix)
    result.fixedVolume = volume
    result.skip()

  def onApplySegmentationButtonClicked(self):
    self.setAxialOrientation()
    self.onQuickSegmentationFinished()

  def onQuickSegmentationFinished(self):
    continueSegmentation = False
    if self.logic.inputMarkupNode.GetNumberOfFiducials() > 3 and self.validPointsForQuickModeSet():

      self.currentIntraopLabel = self.logic.labelMapFromClippingModel(self.logic.currentIntraopVolume)
      labelName = self.logic.currentIntraopVolume.GetName() + '-label'
      self.currentIntraopLabel.SetName(labelName)

      displayNode = self.currentIntraopLabel.GetDisplayNode()
      displayNode.SetAndObserveColorNodeID('vtkMRMLColorTableNode1')

      self.fixedLabelSelector.setCurrentNode(self.currentIntraopLabel)

      self.setTargetVisibility(self.logic.inputMarkupNode, show=False)
      self.logic.clippingModelNode.SetDisplayVisibility(False)
      self.setupScreenAfterSegmentation()
    else:
      if slicer.util.confirmYesNoDisplay("You need to set at least three points with an additional one situated on a distinct slice "
                          "as the algorithm input in order to be able to create a proper segmentation. This step is "
                          "essential for an efficient registration. Do you want to continue using the quick mode?",
                                         windowTitle="SliceTracker"):
        continueSegmentation = True
      else:
        self.logic.deleteClippingData()
    if not continueSegmentation:
      self.setQuickSegmentationModeOFF()
      self.setSegmentationButtons(segmentationActive=False)

  def validPointsForQuickModeSet(self):
    positions = self.getMarkupSlicePositions()
    return min(positions) != max(positions)

  def getMarkupSlicePositions(self):
    markupNode = self.logic.inputMarkupNode
    nOfControlPoints = markupNode.GetNumberOfFiducials()
    positions = []
    pos = [0, 0, 0]
    for i in range(nOfControlPoints):
      markupNode.GetNthFiducialPosition(i, pos)
      positions.append(pos[2])
    return positions

  def setupScreenAfterSegmentation(self):
    self.hideAllLabels()
    self.hideAllTargets()
    self.layoutManager.setLayout(self.LAYOUT_SIDE_BY_SIDE)

    if self.logic.retryMode:
      coverProstateRegResult = self.registrationResults.getMostRecentApprovedCoverProstateRegistration()
      if coverProstateRegResult:
        self.movingVolumeSelector.setCurrentNode(coverProstateRegResult.fixedVolume)
        self.movingLabelSelector.setCurrentNode(coverProstateRegResult.fixedLabel)
        self.fiducialSelector.setCurrentNode(coverProstateRegResult.bSplineTargets)

    self.setupScreenForSegmentationComparison("red", self.movingVolumeSelector.currentNode(),
                                              self.movingLabelSelector.currentNode())
    self.setupScreenForSegmentationComparison("yellow", self.fixedVolumeSelector.currentNode(),
                                              self.fixedLabelSelector.currentNode())
    self.applyRegistrationButton.setEnabled(1 if self.inputsAreSet() else 0)
    self.editorWidgetButton.setEnabled(True)

  def setupScreenForSegmentationComparison(self, viewName, volume, label):
    compositeNode = getattr(self, viewName+"CompositeNode")
    compositeNode.SetReferenceBackgroundVolumeID(volume.GetID())
    compositeNode.SetLabelVolumeID(label.GetID())
    compositeNode.SetLabelOpacity(1)
    logic = getattr(self, viewName+"SliceLogic")
    self.setDefaultFOV(logic, volume)

  def onTrackTargetsButtonClicked(self):
    self.disableTargetMovingMode()
    self.removeSliceAnnotations()
    self.targetTableModel.computeCursorDistances = False
    volume, files = self.logic.getOrCreateVolumeForSeries(self.intraopSeriesSelector.currentText)
    firstRun = self.intraopWatchBox.sourceFile is None
    self.intraopWatchBox.sourceFile = files[0]
    if firstRun:
      self.updateOutputFolder()
    if volume:
      if not self.logic.zFrameRegistrationSuccessful and self.COVER_TEMPLATE in self.intraopSeriesSelector.currentText:
        self.openZFrameRegistrationStep(volume)
        return
      else:
        if self.currentResult is None or \
           self.registrationResults.getMostRecentApprovedCoverProstateRegistration() is None or \
           self.logic.retryMode or self.COVER_PROSTATE in self.intraopSeriesSelector.currentText:
          self.initiateOrRetryTracking(volume)
        else:
          self.repeatRegistrationForCurrentSelection(volume)
        self.activateEvaluationStep()

  def openZFrameRegistrationStep(self, volume):
    self.resetZFrameRegistration()
    self.evaluationMode = True
    self.targetingGroupBox.hide()
    self.zFrameRegistrationGroupBox.show()
    self.setupFourUpView(volume)
    self.redSliceNode.SetSliceVisible(True)
    self.showZFrameModelButton.checked = True
    self.showTemplateButton.checked = True
    self.showTemplatePathButton.checked = True
    self.addROIObserver()
    self.activateCreateROIMode()
    self.addZFrameInstructions()

  def addZFrameInstructions(self, step=1):
    self.zFrameStep = step
    text = self.ZFrame_INSTRUCTION_STEPS[self.zFrameStep]
    self.zFrameInstructionAnnotation = SliceAnnotation(self.redWidget, text, yPos=55, horizontalAlign="center",
                                                       opacity=0.6, color=(0,0.6,0))
    self.zFrameClickObserver = self.redSliceViewInteractor.AddObserver(vtk.vtkCommand.LeftButtonReleaseEvent,
                                                      self.onZFrameStepAccomplished)

  def onZFrameStepAccomplished(self, observee, event):
    self.removeZFrameInstructionAnnotation()
    nextStep = self.zFrameStep + 1
    if nextStep in self.ZFrame_INSTRUCTION_STEPS.keys():
      self.addZFrameInstructions(nextStep)

  def removeZFrameInstructionAnnotation(self):
    if hasattr(self, "zFrameInstructionAnnotation") and self.zFrameInstructionAnnotation:
      self.zFrameInstructionAnnotation.remove()
      self.zFrameInstructionAnnotation = None
    if self.zFrameClickObserver :
      self.redSliceViewInteractor.RemoveObserver(self.zFrameClickObserver)
      self.zFrameClickObserver = None

  def resetZFrameRegistration(self):
    self.applyZFrameRegistrationButton.enabled = False
    self.approveZFrameRegistrationButton.enabled = False
    self.retryZFrameRegistrationButton.enabled = False
    self.removeNodeFromMRMLScene(self.coverTemplateROI)
    self.removeNodeFromMRMLScene(self.zFrameCroppedVolume)
    self.removeNodeFromMRMLScene(self.zFrameLabelVolume)
    self.removeNodeFromMRMLScene(self.zFrameMaskedVolume)
    self.removeNodeFromMRMLScene(self.logic.zFrameTransform)

  def removeNodeFromMRMLScene(self, node):
    if node:
      slicer.mrmlScene.RemoveNode(node)
      node = None

  def removeROIObserver(self):
    if self.roiObserverTag:
      self.roiObserverTag = slicer.mrmlScene.RemoveObserver(self.roiObserverTag)

  def addROIObserver(self):

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAdded(caller, event, calldata):
      node = calldata
      if isinstance(node, slicer.vtkMRMLAnnotationROINode):
        self.removeROIObserver()
        self.coverTemplateROI = node
        self.applyZFrameRegistrationButton.enabled = self.isRegistrationPossible()

    if self.roiObserverTag:
      self.removeROIObserver()
    self.roiObserverTag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.NodeAddedEvent, onNodeAdded)

  def isRegistrationPossible(self):
    return self.coverTemplateROI is not None

  def onApplyZFrameRegistrationButtonClicked(self):
    progress = slicer.util.createProgressDialog(maximum=2, value=1)
    progress.labelText = '\nZFrame registration'
    self.annotationLogic.SetAnnotationLockedUnlocked(self.coverTemplateROI.GetID())
    zFrameTemplateVolume, _ = self.logic.getOrCreateVolumeForSeries(self.intraopSeriesSelector.currentText)
    self.zFrameCroppedVolume = self.logic.createCroppedVolume(zFrameTemplateVolume, self.coverTemplateROI)
    self.zFrameLabelVolume = self.logic.createLabelMapFromCroppedVolume(self.zFrameCroppedVolume)
    self.zFrameMaskedVolume = self.logic.createMaskedVolume(zFrameTemplateVolume, self.zFrameLabelVolume)

    def roundInt(value):
      try:
        return int(round(value))
      except ValueError:
        return 0

    layerLogic = self.redSliceLogic.GetBackgroundLayer()
    p = [0.0,0.0,0.0]
    self.coverTemplateROI.GetXYZ(p)
    xyz = self.redSliceView.convertRASToXYZ(p)
    xyToIJK = layerLogic.GetXYToIJKTransform()
    ijkFloat = xyToIJK.TransformDoublePoint(xyz)
    ijk = [roundInt(value) for value in ijkFloat]
    self.zFrameMaskedVolume.SetName(zFrameTemplateVolume.GetName()+"-label")
    self.logic.runZFrameRegistration(self.zFrameMaskedVolume, startSlice=ijk[2]-4, endSlice=ijk[2]+4)
    self.approveZFrameRegistrationButton.enabled = True
    self.retryZFrameRegistrationButton.enabled = True
    self.applyZFrameRegistrationButton.enabled = False
    progress.setValue(2)
    progress.close()

  def activateCreateROIMode(self):
    mrmlScene = self.annotationLogic.GetMRMLScene()
    selectionNode = mrmlScene.GetNthNodeByClass(0, "vtkMRMLSelectionNode")
    selectionNode.SetReferenceActivePlaceNodeClassName("vtkMRMLAnnotationROINode")
    self.annotationLogic.StartPlaceMode(False)

  def onRetryZFrameRegistrationButtonClicked(self):
    self.annotationLogic.SetAnnotationVisibility(self.coverTemplateROI.GetID())
    self.openZFrameRegistrationStep()

  def onApproveZFrameRegistrationButtonClicked(self):
    self.logic.zFrameRegistrationSuccessful = True
    self.redSliceNode.SetSliceVisible(False)
    self.showZFrameModelButton.checked = False
    self.showTemplateButton.checked = False
    self.showTemplatePathButton.checked = False
    self.annotationLogic.SetAnnotationVisibility(self.coverTemplateROI.GetID())
    self.openTargetingStep()
    self.save()

  def initiateOrRetryTracking(self, volume):
    self.logic.currentIntraopVolume = volume
    self.disableTargetTable()
    self.segmentationGroupBox.show()
    self.editorWidgetButton.setEnabled(False)
    self.activateRegistrationResultsArea(collapsed=True, enabled=False)
    self.configureSegmentationMode()

  def activateRegistrationResultsArea(self, collapsed, enabled):
    self.collapsibleRegistrationArea.collapsed = collapsed
    self.collapsibleRegistrationArea.enabled = enabled

  def disableTargetTable(self):
    self.hideAllTargets()
    self.targetTable.clearSelection()
    self.targetTable.enabled = False

  def repeatRegistrationForCurrentSelection(self, volume):
    logging.debug('Performing Re-Registration')
    self.skipAllUnregisteredPreviousSeries(self.intraopSeriesSelector.currentText)
    self.logic.currentIntraopVolume = volume
    self.onInvokeRegistration(initial=False)
    self.segmentationGroupBox.hide()
    self.activateRegistrationResultsArea(collapsed=False, enabled=True)

  def onEditorGearIconClicked(self):
    if self.editorWidgetParent.visible:
      self.disableEditorWidgetAndResetEditorTool(enabledButton=True)
    else:
      self.editorWidgetParent.show()
      displayNode = self.currentIntraopLabel.GetDisplayNode()
      displayNode.SetAndObserveColorNodeID('vtkMRMLColorTableNode1')
      self.editorParameterNode.SetParameter('effect', 'DrawEffect')
      self.editUtil.setLabel(8)
      self.editUtil.setLabelOutline(1)

  def onInvokeRegistration(self, initial=True):
    self.disableEditorWidgetAndResetEditorTool()
    self.applyRegistrationButton.setEnabled(False)
    self.progress = slicer.util.createProgressDialog(maximum=4, value=1)
    if initial:
      self.logic.applyInitialRegistration(fixedVolume=self.fixedVolumeSelector.currentNode(),
                                          movingVolume=self.movingVolumeSelector.currentNode(),
                                          fixedLabel=self.fixedLabelSelector.currentNode(),
                                          movingLabel=self.movingLabelSelector.currentNode(),
                                          targets=self.fiducialSelector.currentNode(),
                                          progressCallback=self.updateProgressBar)
    else:
      self.logic.applyRegistration(progressCallback=self.updateProgressBar)
    self.progress.close()
    self.progress = None
    for targets in self.currentResult.targets.values():
      if targets:
        targets.SetAndObserveDisplayNodeID(self.logic.intraopTargetDisplayNode.GetID())
    self.finalizeRegistrationStep()
    self.registrationGroupBox.hide()
    logging.debug('Re-Registration is done')

  def updateProgressBar(self, **kwargs):
    if self.progress:
      for key, value in kwargs.iteritems():
        if hasattr(self.progress, key):
          setattr(self.progress, key, value)
    slicer.app.processEvents()

  def finalizeRegistrationStep(self):
    self.targetTableModel.computeCursorDistances = True
    self.targetTable.enabled = True
    self.addNewTargetsToScene()
    self.openRegistrationResultAssessmentStep()
    self.showBSplineResultButton.click()
    self.currentResult.printSummary()
    self.connectCrosshairNode()
    if not self.logic.isVolumeExtentValid(self.currentResult.bSplineVolume):
      slicer.util.infoDisplay("One or more empty volume were created during registration process. You have three options:\n"
                              "1. Skip the registration result \n"
                              "2. Retry with creating a new segmentation \n"
                              "3. Set targets to your preferred position (in Four-Up layout)",
                              title="Action needed: Registration created empty volume(s)", windowTitle="SliceTracker")

  def addNewTargetsToScene(self):
    for targetNode in [targets for targets in self.currentResult.targets.values() if targets]:
      slicer.mrmlScene.AddNode(targetNode)

  def setupRegistrationResultView(self):
    if self.layoutManager.layout == self.LAYOUT_FOUR_UP:
      self.setupRegistrationResultFourUpView()
    else:
      self.setupRegistrationResultSideBySideView()

  def setupRegistrationResultSideBySideView(self):
    self.hideAllLabels()
    self.addSideBySideSliceAnnotations()

    self.refreshViewNodeIDs(self.logic.preopTargets, [self.redSliceNode])
    for targetNode in [targets for targets in self.currentResult.targets.values() if targets]:
      self.refreshViewNodeIDs(targetNode, [self.yellowSliceNode])
    self.setAxialOrientation()

  def setupRegistrationResultFourUpView(self):
    self.hideAllLabels()
    self.addFourUpSliceAnnotations()

    for targetNode in [targets for targets in self.currentResult.targets.values() if targets]:
      self.refreshViewNodeIDs(targetNode, [self.redSliceNode, self.yellowSliceNode, self.greenSliceNode])
    self.setDefaultOrientation()

  def openRegistrationResultAssessmentStep(self):
    self.registrationAssessmentMode = True
    self.updateRegistrationResultSelector()
    self.layoutManager.setLayout(self.LAYOUT_SIDE_BY_SIDE)
    self.setupRegistrationResultView()
    self.segmentationGroupBox.hide()
    self.activateRegistrationResultsArea(collapsed=False, enabled=True)
    self.evaluationButtonsGroupBox.enabled = True

  def refreshViewNodeIDs(self, targets, sliceNodes):
    displayNode = targets.GetDisplayNode()
    if displayNode:
      displayNode.RemoveAllViewNodeIDs()
      for sliceNode in sliceNodes:
        displayNode.AddViewNodeID(sliceNode.GetID())

  def onNewImageDataReceived(self, **kwargs):
    newFileList = kwargs.pop('newList')
    newSeriesNumbers = self.checkForPatientIdSimilarityAndGetSeriesNumbers(newFileList)
    self.updateIntraopSeriesSelectorTable()
    selectedSeries = self.intraopSeriesSelector.currentText
    if selectedSeries != "":
      selectedSeriesNumber = RegistrationResult.getSeriesNumberFromString(selectedSeries)
      if not self.logic.zFrameRegistrationSuccessful and self.COVER_TEMPLATE in selectedSeries and \
                      selectedSeriesNumber in newSeriesNumbers:
        self.onTrackTargetsButtonClicked()
        return

      if not self.evaluationMode and any(seriesText in self.intraopSeriesSelector.currentText for seriesText
                                                  in [self.COVER_TEMPLATE, self.COVER_PROSTATE, self.GUIDANCE_IMAGE]):
        if self.notifyUserAboutNewData:
          dialog = IncomingDataMessageBox()
          self.notifyUserAboutNewDataAnswer, checked = dialog.exec_()
          self.notifyUserAboutNewData = not checked
        if self.notifyUserAboutNewDataAnswer == qt.QMessageBox.AcceptRole:
          self.onTrackTargetsButtonClicked()


from SliceTrackerRegistration import SliceTrackerRegistrationLogic


class SliceTrackerLogic(ScriptedLoadableModuleLogic, ModuleLogicMixin):

  ZFRAME_MODEL_PATH = 'Resources/zframe/zframe-model.vtk'
  ZFRAME_TEMPLATE_CONFIG_FILE_NAME = 'Resources/zframe/ProstateTemplate.csv'
  ZFRAME_MODEL_NAME = 'ZFrameModel'
  ZFRAME_TEMPLATE_NAME = 'NeedleGuideTemplate'
  ZFRAME_TEMPLATE_PATH_NAME = 'NeedleGuideNeedlePath'
  COMPUTED_NEEDLE_MODEL_NAME = 'ComputedNeedleModel'
  MPREVIEW_COLORS_FILE_NAME = 'Resources/Colors/mpReviewColors.csv'

  @property
  def intraopDataDir(self):
    return self._intraopDataDir

  @intraopDataDir.setter
  def intraopDataDir(self, path):
    if os.path.exists(path):
      self._intraopDataDir = path
      self.startIntraopDirListener()

  @property
  def currentResult(self):
      return self.registrationResults.activeResult

  @currentResult.setter
  def currentResult(self, series):
    self.registrationResults.activeResult = series

  @property
  def templateSuccessfulLoaded(self):
    return self.tempModelNode and self.pathModelNode

  @property
  def zFrameSuccessfulLoaded(self):
    return self.zFrameModelNode

  def __init__(self, parent=None):
    ScriptedLoadableModuleLogic.__init__(self, parent)
    self.modulePath = os.path.dirname(slicer.util.modulePath(self.moduleName))
    self.markupsLogic = slicer.modules.markups.logic()
    self.volumesLogic = slicer.modules.volumes.logic()
    self.cropVolumeLogic = slicer.modules.cropvolume.logic()
    self.registrationLogic = SliceTrackerRegistrationLogic()
    self.scalarVolumePlugin = slicer.modules.dicomPlugins['DICOMScalarVolumePlugin']()
    self.defaultTemplateFile = os.path.join(self.modulePath, self.ZFRAME_TEMPLATE_CONFIG_FILE_NAME)
    self.defaultColorFile = os.path.join(self.modulePath, self.MPREVIEW_COLORS_FILE_NAME)
    self.configureTimers()
    self.resetAndInitializeData()

  def resetAndInitializeData(self):

    self.stopWatching()

    self.currentFileCount = 0

    self.inputMarkupNode = None
    self.clippingModelNode = None
    self.seriesList = []
    self.loadableList = {}
    self.alreadyLoadedSeries = {}
    self.storeSCPProcess = None

    self.currentIntraopVolume = None
    self.preopVolume = None
    self.biasCorrectionDone = False
    self.preopLabel = None
    self.preopTargets = None
    self.registrationResults = RegistrationResults()

    self._intraopDataDir = ""

    self._incomingDataCallback = None

    self.retryMode = False
    self.zFrameRegistrationSuccessful = False
    self.zFrameModelNode = None
    self.zFrameTransform = None

    self.showTemplatePath = False
    self.showNeedlePath = False

    self.needleModelNode = None
    self.tempModelNode = None
    self.pathModelNode = None
    self.templateConfig = []
    self.templateMaxDepth = []
    self.pathOrigins = []  ## Origins of needle paths (after transformation by parent transform node)
    self.pathVectors = []  ## Normal vectors of needle paths (after transformation by parent transform node)

    from mpReview import mpReviewLogic
    self.mpReviewColorNode, self.structureNames = mpReviewLogic.loadColorTable(self.defaultColorFile)
    self.clearOldNodes()
    self.loadZFrameModel()
    self.loadTemplateConfigFile()
    self.intraopTargetDisplayNode = self.setupDisplayNode(starBurst=True)
    self.preopTargetDisplayNode = self.setupDisplayNode(starBurst=True)
    self.volumeClipPointsDisplayNode = self.setupDisplayNode()

  def configureTimers(self):
    self.importTimer = qt.QTimer()
    self.importTimer.setInterval(5000)
    self.importTimer.timeout.connect(self.importDICOMSeries)
    self.importTimer.setSingleShot(True)

    self.watchTimer = qt.QTimer()
    self.watchTimer.setInterval(500)
    self.watchTimer.timeout.connect(self.startWatchingIntraop)
    self.watchTimer.setSingleShot(True)

  def clearOldNodes(self):
    self.clearOldNodesByName(self.ZFRAME_TEMPLATE_NAME)
    self.clearOldNodesByName(self.ZFRAME_TEMPLATE_PATH_NAME)
    self.clearOldNodesByName(self.ZFRAME_MODEL_NAME)
    self.clearOldNodesByName(self.COMPUTED_NEEDLE_MODEL_NAME)

  def setupDisplayNode(self, starBurst=False):
    displayNode = slicer.vtkMRMLMarkupsDisplayNode()
    slicer.mrmlScene.AddNode(displayNode)
    displayNode.SetTextScale(0)
    displayNode.SetGlyphScale(2.0)
    if starBurst:
      displayNode.SetGlyphType(slicer.vtkMRMLAnnotationPointDisplayNode.StarBurst2D)
    return displayNode

  def isTrackingPossible(self, series):
    return not (self.registrationResults.registrationResultWasApproved(series) or
                self.registrationResults.registrationResultWasSkipped(series) or
                self.registrationResults.registrationResultWasRejected(series))

  def setReceivedNewImageDataCallback(self, func):
    assert hasattr(func, '__call__')
    self._incomingDataCallback = func

  def createNewCase(self, destinationDir):
    newCaseDirectoryName = "Case"+self.getNextCaseNumber(destinationDir)+datetime.date.today().strftime("-%Y%m%d")
    newCaseDirectory = os.path.join(destinationDir, newCaseDirectoryName)
    os.mkdir(newCaseDirectory)
    os.mkdir(os.path.join(newCaseDirectory, "DICOM"))
    os.mkdir(os.path.join(newCaseDirectory, "DICOM", "Preop"))
    os.mkdir(os.path.join(newCaseDirectory, "DICOM", "Intraop"))
    os.mkdir(os.path.join(newCaseDirectory, "mpReviewPreprocessed"))
    os.mkdir(os.path.join(newCaseDirectory, "SliceTrackerOutputs"))
    return newCaseDirectory

  def isCaseDirectoryValid(self, directory):
    return os.path.exists(os.path.join(directory, "DICOM")) and os.path.exists(os.path.join(directory, "DICOM", "Preop")) \
           and os.path.exists(os.path.join(directory, "DICOM", "Intraop")) \
               and os.path.exists(os.path.join(directory, "mpReviewPreprocessed"))

  def hasCaseBeenCompleted(self, directory):
    return ".completed" in os.listdir(directory)

  def getNextCaseNumber(self, destinationDir):
    caseNumber = 0
    for dirname in [dirname for dirname in os.listdir(destinationDir)
                     if os.path.isdir(os.path.join(destinationDir, dirname)) and dirname.startswith("Case")]:
      number = int(dirname.split("-")[0].replace("Case",""))
      caseNumber = caseNumber if caseNumber > number else number
    caseNumber = str(caseNumber+1)
    while len(caseNumber) < 3 :
      caseNumber = "0" + caseNumber
    return caseNumber

  def completeCase(self, directory):
    self.stopStoreSCP()
    if os.path.exists(directory):
      if self.getDirectorySize(directory) > 0:
        open(os.path.join(directory, ".completed"), 'a').close()

  def closeCase(self, directory):
    if os.path.exists(directory):
      if self.getDirectorySize(directory) == 0:
        shutil.rmtree(directory)

  def getFirstStudyFromMpReviewPreprocessed(self, directory):
    # TODO: if several studies are available provide a dropdown or anything similar for choosing
    directoryNames = [x[0] for x in os.walk(directory)]
    assert len(directoryNames) > 1
    return directoryNames[1]

  def loadPreopImageAndSegmentation(self, seriesMap):
    self.preopImagePath = None
    self.preopSegmentationPath = None
    for series in seriesMap:
      seriesName = str(seriesMap[series]['LongName'])
      logging.debug('series Number ' + series + ' ' + seriesName)
      if re.search("ax", str(seriesName), re.IGNORECASE) and re.search("t2", str(seriesName), re.IGNORECASE):
        logging.debug(' FOUND THE SERIES OF INTEREST, ITS ' + seriesName)
        logging.debug(' LOCATION OF VOLUME : ' + str(seriesMap[series]['NRRDLocation']))

        path = os.path.join(seriesMap[series]['NRRDLocation'])
        logging.debug(' LOCATION OF IMAGE path : ' + str(path))

        segmentationPath = os.path.dirname(os.path.dirname(path))
        segmentationPath = os.path.join(segmentationPath, 'Segmentations')
        logging.debug(' LOCATION OF SEGMENTATION path : ' + segmentationPath)

        if not self.preopSegmentationPath and os.path.exists(segmentationPath) and os.listdir(segmentationPath):
          self.preopImagePath = seriesMap[series]['NRRDLocation']
          self.preopSegmentationPath = segmentationPath

  def loadT2Label(self):
    mostRecentFilename = self.getMostRecentWholeGlandSegmentation(self.preopSegmentationPath)
    success = False
    if mostRecentFilename:
      filename = os.path.join(self.preopSegmentationPath, mostRecentFilename)
      success, self.preopLabel = slicer.util.loadLabelVolume(filename, returnNode=True)
      if success:
        self.preopLabel.SetName('t2-label')
    return success

  def loadPreopVolume(self):
    success, self.preopVolume = slicer.util.loadVolume(self.preopImagePath, returnNode=True)
    if success:
      self.preopVolume.SetName('VOLUME-PREOP')
    return success

  def loadPreopTargets(self):
    mostRecentTargets = self.getMostRecentTargetsFile(self.preopTargetsPath)
    success = False
    if mostRecentTargets:
      filename = os.path.join(self.preopTargetsPath, mostRecentTargets)
      success, self.preopTargets = slicer.util.loadMarkupsFiducialList(filename, returnNode=True)
      if success:
        self.preopTargets.SetName('targets-PREOP')
        self.markupsLogic.SetAllMarkupsLocked(self.preopTargets, True)
    return success

  def save(self, outputDir):
    self.createDirectory(outputDir)

    successfullySavedData = ["The following data was successfully saved:\n"]
    failedSaveOfData = ["The following data failed to saved:\n"]

    def saveIntraopSegmentation():
      intraopLabel = self.registrationResults.intraopLabel
      if intraopLabel:
        seriesNumber = intraopLabel.GetName().split(":")[0]
        success, name = self.saveNodeData(intraopLabel, outputDir, '.nrrd', name=seriesNumber+"-LABEL")
        self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

        if self.clippingModelNode:
          success, name = self.saveNodeData(self.clippingModelNode, outputDir, '.vtk', name=seriesNumber+"-MODEL")
          self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

        if self.inputMarkupNode:
          success, name = self.saveNodeData(self.inputMarkupNode, outputDir, '.fcsv',
                                            name=seriesNumber+"-VolumeClip_points")
          self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

    def saveOriginalTargets():
      originalTargets = self.registrationResults.originalTargets
      if originalTargets:
        success, name = self.saveNodeData(originalTargets, outputDir, '.fcsv', name="PreopTargets")
        self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

    def saveBiasCorrectionResult():
      if self.biasCorrectionDone:
        success, name = self.saveNodeData(self.preopVolume, outputDir, '.nrrd')
        self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

    def saveZFrameTransformation():
      if self.zFrameTransform:
        success, name = self.saveNodeData(self.zFrameTransform, outputDir, '.h5')
        self.handleSaveNodeDataReturn(success, name, successfullySavedData, failedSaveOfData)

    saveIntraopSegmentation()
    saveOriginalTargets()
    saveBiasCorrectionResult()
    saveZFrameTransformation()

    savedSuccessfully, failedToSave = self.registrationResults.save(outputDir)
    successfullySavedData += savedSuccessfully
    failedSaveOfData += failedToSave

    messageOutput = ""
    for messageList in [successfullySavedData, failedSaveOfData] :
      if len(messageList) > 1:
        for message in messageList:
          messageOutput += message + "\n"
    return messageOutput if messageOutput != "" else "There is nothing to be saved yet."

  def stopStoreSCP(self):
    if self.storeSCPProcess:
      self.storeSCPProcess.stop()

  def startStoreSCP(self, callback=None):
    self.storeSCPCallback = callback
    self.stopStoreSCP()
    try:
      self.storeSCPProcess = DICOMLib.DICOMStoreSCPProcess(incomingDataDir=self.intraopDataDir)
      self.storeSCPProcess.start()
      if self.storeSCPProcess.process:
        self.onDICOMStoreSCPProcessStateChanged(self.storeSCPProcess.process.state())
        self.storeSCPProcess.process.connect('stateChanged(QProcess::ProcessState)', self.onDICOMStoreSCPProcessStateChanged)
    except UserWarning as message:
      self.storeSCPCallback("Could not start listener:\n %s" % message, windowTitle="DICOM")

  def onDICOMStoreSCPProcessStateChanged(self, newState):
    messageCodes = {0:"DICOM StoreSCP not running", 1:"DICOM StoreSCP starting", 2:"DICOM StoreSCP running"}
    slicer.util.showStatusMessage(messageCodes[newState] if newState in messageCodes.keys() else "")

  def getSeriesInfoFromXML(self, f):
    import xml.dom.minidom
    dom = xml.dom.minidom.parse(f)
    number = self.findElement(dom, 'SeriesNumber')
    name = self.findElement(dom, 'SeriesDescription')
    name = name.replace('-', '')
    name = name.replace('(', '')
    name = name.replace(')', '')
    return number, name

  def findElement(self, dom, name):
    els = dom.getElementsByTagName('element')
    for e in els:
      if e.getAttribute('name') == name:
        return e.childNodes[0].nodeValue

  def getMostRecentWholeGlandSegmentation(self, path):
    return self.getMostRecentFile(path, "nrrd", filter="WholeGland")

  def getMostRecentTargetsFile(self, path):
    return self.getMostRecentFile(path, "fcsv")

  def getMostRecentFile(self, path, fileType, filter=None):
    assert type(fileType) is str
    files = [f for f in os.listdir(path) if f.endswith(fileType)]
    if len(files) == 0:
      return None
    mostRecent = None
    storedTimeStamp = 0
    for filename in files:
      if filter and not filter in filename:
        continue
      actualFileName = filename.split(".")[0]
      timeStamp = int(actualFileName.split("-")[-1])
      if timeStamp > storedTimeStamp:
        mostRecent = filename
        storedTimeStamp = timeStamp
    return mostRecent

  def applyBiasCorrection(self):
    outputVolume = slicer.vtkMRMLScalarVolumeNode()
    outputVolume.SetName('VOLUME-PREOP-N4')
    slicer.mrmlScene.AddNode(outputVolume)
    params = {'inputImageName': self.preopVolume.GetID(),
              'maskImageName': self.preopLabel.GetID(),
              'outputImageName': outputVolume.GetID(),
              'numberOfIterations': '500,400,300'}

    slicer.cli.run(slicer.modules.n4itkbiasfieldcorrection, None, params, wait_for_completion=True)
    self.preopVolume = outputVolume
    self.biasCorrectionDone = True

  def applyInitialRegistration(self, fixedVolume, movingVolume, fixedLabel, movingLabel, targets, progressCallback=None):

    if not self.retryMode:
      self.registrationResults = RegistrationResults()
    self.retryMode = False

    self.generateNameAndCreateRegistrationResult(fixedVolume)

    parameterNode = slicer.vtkMRMLScriptedModuleNode()
    parameterNode.SetAttribute('FixedImageNodeID', fixedVolume.GetID())
    parameterNode.SetAttribute('FixedLabelNodeID', fixedLabel.GetID())
    parameterNode.SetAttribute('MovingImageNodeID', movingVolume.GetID())
    parameterNode.SetAttribute('MovingLabelNodeID', movingLabel.GetID())
    parameterNode.SetAttribute('TargetsNodeID', targets.GetID())

    self.registrationLogic.run(parameterNode, progressCallback=progressCallback)

  def generateNameAndCreateRegistrationResult(self, fixedVolume):
    name, suffix = self.getRegistrationResultNameAndGeneratedSuffix(fixedVolume.GetName())
    result = self.registrationResults.createResult(name + suffix)
    result.suffix = suffix
    self.registrationLogic.registrationResult = result

  def applyRegistration(self, progressCallback=None):

    coverProstateRegResult = self.registrationResults.getMostRecentApprovedCoverProstateRegistration()
    lastRigidTfm = self.registrationResults.getLastApprovedRigidTransformation()

    self.generateNameAndCreateRegistrationResult(self.currentIntraopVolume)
    parameterNode = slicer.vtkMRMLScriptedModuleNode()
    parameterNode.SetAttribute('FixedImageNodeID', self.currentIntraopVolume.GetID())
    parameterNode.SetAttribute('FixedLabelNodeID', coverProstateRegResult.fixedLabel.GetID())
    parameterNode.SetAttribute('MovingImageNodeID', coverProstateRegResult.fixedVolume.GetID())
    parameterNode.SetAttribute('MovingLabelNodeID', coverProstateRegResult.fixedLabel.GetID())
    parameterNode.SetAttribute('TargetsNodeID', coverProstateRegResult.approvedTargets.GetID())
    if lastRigidTfm:
      parameterNode.SetAttribute('InitialTransformNodeID', lastRigidTfm.GetID())

    self.registrationLogic.runReRegistration(parameterNode, progressCallback=progressCallback)

  def getRegistrationResultNameAndGeneratedSuffix(self, name):
    nOccurences = sum([1 for result in self.registrationResults.getResultsAsList() if name in result.name])
    suffix = ""
    if nOccurences:
      suffix = "_Retry_" + str(nOccurences)
    return name, suffix

  def startIntraopDirListener(self):
    self.currentFileList = []
    self.lastFileCount = len(self.getFileList(self._intraopDataDir))
    self.currentFileCount = self.lastFileCount
    self.importDICOMSeries()
    self.stopWatching()
    self.startWatchingIntraop()

  def stopWatching(self):
    self.importTimer.stop()
    self.watchTimer.stop()

  def startWatchingIntraop(self):
    # TODO: should rather start watching DICOM database and check if there are new series that can be loaded
    self.currentFileCount = len(self.getFileList(self._intraopDataDir))
    if self.lastFileCount != self.currentFileCount:
      self.importTimer.start()
    self.lastFileCount = self.currentFileCount
    self.watchTimer.start()

  def createLoadableFileListForSeries(self, selectedSeries):
    selectedSeriesNumber = RegistrationResult.getSeriesNumberFromString(selectedSeries)
    self.loadableList[selectedSeries] = []
    for dcm in self.getFileList(self._intraopDataDir):
      currentFile = os.path.join(self._intraopDataDir, dcm)
      currentSeriesNumber = int(self.getDICOMValue(currentFile, DICOMTAGS.SERIES_NUMBER))
      if currentSeriesNumber and currentSeriesNumber == selectedSeriesNumber:
        self.loadableList[selectedSeries].append(currentFile)

  def getOrCreateVolumeForSeries(self, series):
    files = self.loadableList[series]
    try:
      volume = self.alreadyLoadedSeries[series]
    except KeyError:
      loadables = self.scalarVolumePlugin.examine([files])
      volume = self.scalarVolumePlugin.load(loadables[0])
      volume.SetName(loadables[0].name)
      self.alreadyLoadedSeries[series] = volume
    return volume, files

  def importDICOMSeries(self):
    indexer = ctk.ctkDICOMIndexer()
    db = slicer.dicomDatabase

    fileList = self.getFileList(self._intraopDataDir)

    newFileList = list(set(fileList) - set(self.currentFileList))

    for currentFile in newFileList:
      currentFile = os.path.join(self._intraopDataDir, currentFile)
      indexer.addFile(db, currentFile, None)
      series = self.makeSeriesNumberDescription(currentFile)
      if series and series not in self.seriesList and self.isDICOMSeriesEligible(series):
        self.seriesList.append(series)
        self.createLoadableFileListForSeries(series)

    indexer.addDirectory(db, self._intraopDataDir)
    indexer.waitForImportFinished()

    self.seriesList = sorted(self.seriesList, key=lambda series: RegistrationResult.getSeriesNumberFromString(series))

    if self._incomingDataCallback and len(newFileList) > 0 and \
                    len(self.getFileList(self._intraopDataDir)) == self.currentFileCount:
      self._incomingDataCallback(newList=newFileList)

    self.currentFileList = fileList

  def deleteSeriesFromSeriesList(self, seriesNumber):
    for series in self.seriesList:
      currentSeriesNumber = RegistrationResult.getSeriesNumberFromString(series)
      if currentSeriesNumber == seriesNumber:
        self.seriesList.remove(series)

  def extractDateFromDICOMFile(self, currentFile, tag=DICOMTAGS.STUDY_DATE):
    extractedDate = self.getDICOMValue(currentFile, tag)
    if extractedDate:
      formatted = datetime.date(int(extractedDate[0:4]), int(extractedDate[4:6]), int(extractedDate[6:8]))
      return formatted.strftime("%Y-%b-%d")
    else:
      return ""

  def isDICOMSeriesEligible(self, series):
    return SliceTrackerConstants.COVER_PROSTATE in series or SliceTrackerConstants.COVER_TEMPLATE in series or \
           SliceTrackerConstants.GUIDANCE_IMAGE in series

  def makeSeriesNumberDescription(self, dicomFile):
    seriesDescription = self.getDICOMValue(dicomFile, DICOMTAGS.SERIES_DESCRIPTION)
    seriesNumber = self.getDICOMValue(dicomFile, DICOMTAGS.SERIES_NUMBER)
    seriesNumberDescription = None
    if seriesDescription and seriesNumber:
      seriesNumberDescription = seriesNumber + ": " + seriesDescription
    return seriesNumberDescription

  def getTargetPositions(self, registeredTargets):
    number_of_targets = registeredTargets.GetNumberOfFiducials()
    target_positions = []
    for target in range(number_of_targets):
      target_position = [0.0, 0.0, 0.0]
      registeredTargets.GetNthFiducialPosition(target, target_position)
      target_positions.append(target_position)
    logging.debug('target_positions are ' + str(target_positions))
    return target_positions

  def getNeedleTipTargetDistance2D(self, target_position, needleTip_position):
    x = abs(target_position[0] - needleTip_position[0])
    y = abs(target_position[1] - needleTip_position[1])
    return [x, y]

  def getNeedleTipTargetDistance3D(self, target_position, needleTip_position):
    rulerNode = slicer.vtkMRMLAnnotationRulerNode()
    rulerNode.SetPosition1(target_position)
    rulerNode.SetPosition2(needleTip_position)
    distance_3D = rulerNode.GetDistanceMeasurement()
    return distance_3D

  def run(self):
    return True

  def runQuickSegmentationMode(self):
    self.setVolumeClipUserMode()
    self.placeFiducials()

  def setVolumeClipUserMode(self):
    lm = slicer.app.layoutManager()
    for widgetName in ['Red', 'Green', 'Yellow']:
      slice = lm.sliceWidget(widgetName)
      sliceLogic = slice.sliceLogic()
      sliceLogic.FitSliceToAll()

    # set the mouse mode into Markups fiducial placement
    placeModePersistence = 1
    self.markupsLogic.StartPlaceMode(placeModePersistence)

  def updateModel(self, observer, caller):
    import VolumeClipWithModel
    clipLogic = VolumeClipWithModel.VolumeClipWithModelLogic()
    clipLogic.updateModelFromMarkup(self.inputMarkupNode, self.clippingModelNode)

  def deleteClippingData(self):
    slicer.mrmlScene.RemoveNode(self.clippingModelNode)
    logging.debug('deleted ModelNode')
    slicer.mrmlScene.RemoveNode(self.inputMarkupNode)
    logging.debug('deleted inputMarkupNode')

  def placeFiducials(self):
    self.clippingModelNode = slicer.vtkMRMLModelNode()
    self.clippingModelNode.SetName('clipModelNode')
    slicer.mrmlScene.AddNode(self.clippingModelNode)
    self.createClippingModelDisplayNode()
    self.createMarkupAndDisplayNodeForFiducials()
    self.inputMarkupNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.updateModel)
    self.inputMarkupNode.SetAndObserveDisplayNodeID(self.volumeClipPointsDisplayNode.GetID())

  def createMarkupAndDisplayNodeForFiducials(self):
    self.inputMarkupNode = slicer.vtkMRMLMarkupsFiducialNode()
    self.inputMarkupNode.SetName('inputMarkupNode')
    slicer.mrmlScene.AddNode(self.inputMarkupNode)

  def createClippingModelDisplayNode(self):
    clippingModelDisplayNode = slicer.vtkMRMLModelDisplayNode()
    clippingModelDisplayNode.SetSliceIntersectionThickness(3)
    clippingModelDisplayNode.SetColor([0.200, 0.800, 0.000]) # green for glant
    clippingModelDisplayNode.BackfaceCullingOff()
    clippingModelDisplayNode.SliceIntersectionVisibilityOn()
    clippingModelDisplayNode.SetOpacity(0.3)
    slicer.mrmlScene.AddNode(clippingModelDisplayNode)
    self.clippingModelNode.SetAndObserveDisplayNodeID(clippingModelDisplayNode.GetID())

  def labelMapFromClippingModel(self, inputVolume):
    outputLabelMap = slicer.vtkMRMLLabelMapVolumeNode()
    slicer.mrmlScene.AddNode(outputLabelMap)

    params = {'sampleDistance': 0.1, 'labelValue': 8, 'InputVolume': inputVolume.GetID(),
              'surface': self.clippingModelNode.GetID(), 'OutputVolume': outputLabelMap.GetID()}

    logging.debug(params)
    slicer.cli.run(slicer.modules.modeltolabelmap, None, params, wait_for_completion=True)

    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").SetUseLabelOutline(True)
    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeYellow").SetUseLabelOutline(True)

    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").RotateToVolumePlane(outputLabelMap)
    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeYellow").RotateToVolumePlane(outputLabelMap)

    return outputLabelMap

  def loadZFrameModel(self):
    zFrameModelPath = os.path.join(self.modulePath, self.ZFRAME_MODEL_PATH)
    if not self.zFrameModelNode:
      _, self.zFrameModelNode = slicer.util.loadModel(zFrameModelPath, returnNode=True)
      self.zFrameModelNode.SetName(self.ZFRAME_MODEL_NAME)
      slicer.mrmlScene.AddNode(self.zFrameModelNode)
      modelDisplayNode = self.zFrameModelNode.GetDisplayNode()
      modelDisplayNode.SetColor(1, 1, 0)
    self.zFrameModelNode.SetDisplayVisibility(False)

  def clearOldNodesByName(self, name):
    collection = slicer.mrmlScene.GetNodesByName(name)
    for index in range(collection.GetNumberOfItems()):
      slicer.mrmlScene.RemoveNode(collection.GetItemAsObject(index))

  def createLabelMapFromCroppedVolume(self, volume):
    labelVolume = self.volumesLogic.CreateAndAddLabelVolume(volume, "labelmap")
    imagedata = labelVolume.GetImageData()
    imageThreshold = vtk.vtkImageThreshold()
    imageThreshold.SetInputData(imagedata)
    imageThreshold.ThresholdBetween(0, 2000)
    imageThreshold.SetInValue(1)
    imageThreshold.Update()
    labelVolume.SetAndObserveImageData(imageThreshold.GetOutput())
    return labelVolume

  def createCroppedVolume(self, inputVolume, roi):
    cropVolumeParameterNode = slicer.vtkMRMLCropVolumeParametersNode()
    cropVolumeParameterNode.SetROINodeID(roi.GetID())
    cropVolumeParameterNode.SetInputVolumeNodeID(inputVolume.GetID())
    cropVolumeParameterNode.SetVoxelBased(True)
    self.cropVolumeLogic.Apply(cropVolumeParameterNode)
    croppedVolume = slicer.mrmlScene.GetNodeByID(cropVolumeParameterNode.GetOutputVolumeNodeID())
    return croppedVolume

  def createMaskedVolume(self, inputVolume, labelVolume):
    maskedVolume = slicer.vtkMRMLScalarVolumeNode()
    maskedVolume.SetName("maskedTemplateVolume")
    slicer.mrmlScene.AddNode(maskedVolume)
    params = {'InputVolume': inputVolume, 'MaskVolume': labelVolume, 'OutputVolume': maskedVolume}
    slicer.cli.run(slicer.modules.maskscalarvolume, None, params, wait_for_completion=True)
    return maskedVolume

  def runZFrameRegistration(self, inputVolume, startSlice, endSlice):
    # TODO: create configfile that chooses the Registration algorithm to use
    registration = ZFrameRegistration(inputVolume)
    registration.runRegistration(start=startSlice, end=endSlice)
    self.zFrameTransform = registration.getOutputTransformation()
    self.applyZFrameTransform(self.zFrameTransform)

  def loadTemplateConfigFile(self):
    self.templateIndex = []
    self.templateConfig = []

    reader = csv.reader(open(self.defaultTemplateFile, 'rb'))
    try:
      next(reader)
      for row in reader:
        self.templateIndex.append(row[0:2])
        self.templateConfig.append([float(row[2]), float(row[3]), float(row[4]),
                                    float(row[5]), float(row[6]), float(row[7]),
                                    float(row[8])])
    except csv.Error as e:
      print('file %s, line %d: %s' % (self.defaultTemplateFile, reader.line_num, e))
      return

    self.createTemplateAndNeedlePathModel()
    self.setTemplateVisibility(0)
    self.setTemplatePathVisibility(0)
    self.setNeedlePathVisibility(0)
    self.updateTemplateVectors()

  def createTemplateAndNeedlePathModel(self):

    self.templatePathVectors = []
    self.templatePathOrigins = []

    self.checkAndCreateTemplateModelNode()
    self.checkAndCreatePathModelNode()

    pathModelAppend = vtk.vtkAppendPolyData()
    templateModelAppend = vtk.vtkAppendPolyData()

    for row in self.templateConfig:
      p, n = self.extractPointsAndNormalVectors(row)

      tempTubeFilter = self.createTubeFilter(p[0], p[1], radius=1.0, numSides=18)
      templateModelAppend.AddInputData(tempTubeFilter.GetOutput())
      templateModelAppend.Update()

      pathTubeFilter = self.createTubeFilter(p[0], p[2], radius=0.8, numSides=18)
      pathModelAppend.AddInputData(pathTubeFilter.GetOutput())
      pathModelAppend.Update()

      self.templatePathOrigins.append([row[0], row[1], row[2], 1.0])
      self.templatePathVectors.append([n[0], n[1], n[2], 1.0])
      self.templateMaxDepth.append(row[6])

    self.tempModelNode.SetAndObservePolyData(templateModelAppend.GetOutput())
    modelDisplayNode = self.tempModelNode.GetDisplayNode()
    modelDisplayNode.SetColor(0.5,0,1)
    self.pathModelNode.SetAndObservePolyData(pathModelAppend.GetOutput())
    modelDisplayNode = self.pathModelNode.GetDisplayNode()
    modelDisplayNode.SetColor(0.8,0.5,1)

  def createNeedleModelNode(self, start, end):
    self.removeNeedleModelNode()
    self.needleModelNode = self.createModelNode(self.COMPUTED_NEEDLE_MODEL_NAME)
    modelDisplayNode = self.setAndObserveDisplayNode(self.needleModelNode)
    modelDisplayNode.SetColor(0, 1, 0)
    pathTubeFilter = self.createTubeFilter(start, end, radius=1.0, numSides=18)
    self.needleModelNode.SetAndObservePolyData(pathTubeFilter.GetOutput())
    self.setNeedlePathVisibility(self.showNeedlePath)

  def removeNeedleModelNode(self):
    if self.needleModelNode:
      slicer.mrmlScene.RemoveNode(self.needleModelNode)
    self.clearOldNodesByName(self.COMPUTED_NEEDLE_MODEL_NAME)

  def extractPointsAndNormalVectors(self, row):
    p1 = numpy.array(row[0:3])
    p2 = numpy.array(row[3:6])
    v = p2-p1
    nl = numpy.linalg.norm(v)
    n = v/nl  # normal vector
    l = row[6]
    p3 = p1 + l * n
    return [p1, p2, p3], n

  def createTubeFilter(self, start, end, radius, numSides):
    lineSource = vtk.vtkLineSource()
    lineSource.SetPoint1(start)
    lineSource.SetPoint2(end)
    tubeFilter = vtk.vtkTubeFilter()

    tubeFilter.SetInputConnection(lineSource.GetOutputPort())
    tubeFilter.SetRadius(radius)
    tubeFilter.SetNumberOfSides(numSides)
    tubeFilter.CappingOn()
    tubeFilter.Update()
    return tubeFilter

  def checkAndCreatePathModelNode(self):
    if self.pathModelNode is None:
      self.pathModelNode = self.createModelNode(self.ZFRAME_TEMPLATE_PATH_NAME)
      self.setAndObserveDisplayNode(self.pathModelNode)

  def checkAndCreateTemplateModelNode(self):
    if self.tempModelNode is None:
      self.tempModelNode = self.createModelNode(self.ZFRAME_TEMPLATE_NAME)
      self.setAndObserveDisplayNode(self.tempModelNode)
      self.modelNodeTag = self.tempModelNode.AddObserver(slicer.vtkMRMLTransformableNode.TransformModifiedEvent,
                                                         self.updateTemplateVectors)

  def applyZFrameTransform(self, transform):
    for node in [node for node in [self.pathModelNode, self.tempModelNode, self.zFrameModelNode, self.needleModelNode] if node]:
      node.SetAndObserveTransformNodeID(transform.GetID())

  def setModelVisibility(self, node, visible):
    dnode = node.GetDisplayNode()
    if dnode is not None:
      dnode.SetVisibility(visible)

  def setModelSliceIntersectionVisibility(self, node, visible):
    dnode = node.GetDisplayNode()
    if dnode is not None:
      dnode.SetSliceIntersectionVisibility(visible)

  def setZFrameVisibility(self, visibility):
    self.setModelVisibility(self.zFrameModelNode, visibility)
    self.setModelSliceIntersectionVisibility(self.zFrameModelNode, visibility)

  def setTemplateVisibility(self, visibility):
    self.setModelVisibility(self.tempModelNode, visibility)

  def setTemplatePathVisibility(self, visibility):
    self.showTemplatePath = visibility
    self.setModelVisibility(self.pathModelNode, visibility)
    self.setModelSliceIntersectionVisibility(self.pathModelNode, visibility)

  def setNeedlePathVisibility(self, visibility):
    self.showNeedlePath = visibility
    if self.needleModelNode:
      self.setModelVisibility(self.needleModelNode, visibility)
      self.setModelSliceIntersectionVisibility(self.needleModelNode, visibility)

  def updateTemplateVectors(self, observee=None, event=None):
    if self.tempModelNode is None:
      return

    trans = vtk.vtkMatrix4x4()
    transformNode = self.tempModelNode.GetParentTransformNode()
    if transformNode is not None:
      transformNode.GetMatrixTransformToWorld(trans)
    else:
      trans.Identity()

    # Calculate offset
    zero = [0.0, 0.0, 0.0, 1.0]
    offset = trans.MultiplyDoublePoint(zero)

    self.pathOrigins = []
    self.pathVectors = []

    for i, orig in enumerate(self.templatePathOrigins):
      torig = trans.MultiplyDoublePoint(orig)
      self.pathOrigins.append(numpy.array(torig[0:3]))
      vec = self.templatePathVectors[i]
      tvec = trans.MultiplyDoublePoint(vec)
      self.pathVectors.append(numpy.array([tvec[0]-offset[0], tvec[1]-offset[1], tvec[2]-offset[2]]))
      i += 1

  def computeNearestPath(self, pos):
    minMag2 = numpy.Inf
    minDepth = 0.0
    minIndex = -1
    needleStart = None
    needleEnd = None

    p = numpy.array(pos)
    for i, orig in enumerate(self.pathOrigins):
      vec = self.pathVectors[i]
      op = p - orig
      aproj = numpy.inner(op, vec)
      perp = op-aproj*vec
      mag2 = numpy.vdot(perp, perp)
      if mag2 < minMag2:
        minMag2 = mag2
        minIndex = i
        minDepth = aproj
      i += 1

    indexX = '--'
    indexY = '--'
    inRange = False

    if minIndex != -1:
      indexX = self.templateIndex[minIndex][0]
      indexY = self.templateIndex[minIndex][1]
      if 0 < minDepth < self.templateMaxDepth[minIndex]:
        inRange = True
        needleStart, needleEnd = self.getNeedleStartEndPointFromPathOrigins(minIndex)
      else:
        self.removeNeedleModelNode()

    return needleStart, needleEnd, indexX, indexY, minDepth, inRange

  def getNeedleStartEndPointFromPathOrigins(self, index):
    start = self.pathOrigins[index]
    v = self.pathVectors[index]
    nl = numpy.linalg.norm(v)
    n = v / nl  # normal vector
    l = self.templateMaxDepth[index]
    end = start + l * n
    return start, end


class SliceTrackerTest(ScriptedLoadableModuleTest):
  """
  This is the test case for your scripted module.
  Uses ScriptedLoadableModuleTest base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setUp(self):
    """ Do whatever is needed to reset the state - typically a scene clear will be enough.
    """
    slicer.mrmlScene.Clear(0)

  def runTest(self):
    """Run as few or as many tests as needed here.
    """
    self.setUp()
    self.test_SliceTracker1()

  def test_SliceTracker1(self):
    """ Ideally you should have several levels of tests.  At the lowest level
    tests should exercise the functionality of the logic with different inputs
    (both valid and invalid).  At higher levels your tests should emulate the
    way the user would interact with your code and confirm that it still works
    the way you intended.
    One of the most important features of the tests is that it should alert other
    developers when their changes will have an impact on the behavior of your
    module.  For example, if a developer removes a feature that you depend on,
    your test should break so they know that the feature is needed.
    """
    logging.debug(' ___ performing selfTest ___ ')


class IncomingDataMessageBox(ExtendedQMessageBox):

  def __init__(self, parent=None):
    super(IncomingDataMessageBox, self).__init__(parent)
    self.setWindowTitle("Dialog with CheckBox")
    self.setText("New data has been received. What would you do?")
    self.setIcon(qt.QMessageBox.Question)
    trackButton =  self.addButton(qt.QPushButton('Track targets'), qt.QMessageBox.AcceptRole)
    self.addButton(qt.QPushButton('Postpone'), qt.QMessageBox.NoRole)
    self.setDefaultButton(trackButton)


class RatingWindow(qt.QWidget, ModuleWidgetMixin):

  @property
  def maximumValue(self):
    return self._maximumValue

  @maximumValue.setter
  def maximumValue(self, value):
    if value < 1:
      raise ValueError("The maximum rating value cannot be less than 1.")
    else:
      self._maximumValue = value

  def __init__(self, maximumValue, text="Please rate the registration result:", *args):
    qt.QWidget.__init__(self, *args)
    self.maximumValue = maximumValue
    self.text = text
    self.iconPath = os.path.join(os.path.dirname(sys.modules[self.__module__].__file__), 'Resources/Icons')
    self.setupIcons()
    self.setLayout(qt.QGridLayout())
    self.setWindowFlags(qt.Qt.WindowStaysOnTopHint | qt.Qt.FramelessWindowHint)
    self.setupElements()
    self.connectButtons()
    self.showRatingValue = True

  def __del__(self):
    self.disconnectButtons()

  def isRatingEnabled(self):
    return not self.disableWidgetCheckbox.checked

  def setupIcons(self):
    self.filledStarIcon = self.createIcon("icon-star-filled.png", self.iconPath)
    self.unfilledStarIcon = self.createIcon("icon-star-unfilled.png", self.iconPath)

  def show(self, disableWidget=None, callback=None):
    self.disabledWidget = disableWidget
    if disableWidget:
      disableWidget.enabled = False
    qt.QWidget.show(self)
    self.ratingScore = None
    self.callback = callback

  def setupElements(self):
    self.layout().addWidget(qt.QLabel(self.text), 0, 0)
    self.ratingButtonGroup = qt.QButtonGroup()
    for rateValue in range(1, self.maximumValue+1):
      attributeName = "button"+str(rateValue)
      setattr(self, attributeName, self.createButton('', icon=self.unfilledStarIcon))
      self.ratingButtonGroup.addButton(getattr(self, attributeName), rateValue)

    for button in list(self.ratingButtonGroup.buttons()):
      button.setCursor(qt.Qt.PointingHandCursor)

    self.ratingLabel = self.createLabel("")
    row = self.createHLayout(list(self.ratingButtonGroup.buttons()) + [self.ratingLabel])
    self.layout().addWidget(row, 1, 0)

    self.disableWidgetCheckbox = qt.QCheckBox("Don't display this window again")
    self.disableWidgetCheckbox.checked = False
    self.layout().addWidget(self.disableWidgetCheckbox, 2, 0)

  def connectButtons(self):
    self.ratingButtonGroup.connect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.installEventFilter(self)

  def disconnectButtons(self):
    self.ratingButtonGroup.disconnect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.removeEventFilter(self)

  def eventFilter(self, obj, event):
    if obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverEnter:
      self.onHoverEvent(obj)
    elif obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverLeave:
      self.onLeaveEvent()
    return qt.QWidget.eventFilter(self, obj, event)

  def onLeaveEvent(self):
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.unfilledStarIcon

  def onHoverEvent(self, obj):
    ratingValue = 0
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.filledStarIcon
      ratingValue += 1
      if obj is button:
        break
    if self.showRatingValue:
      self.ratingLabel.setText(str(ratingValue))

  def onRatingButtonClicked(self, buttonId):
    self.ratingScore = buttonId
    if self.disabledWidget:
      self.disabledWidget.enabled = True
      self.disabledWidget = None
    if self.callback:
      self.callback(self.ratingScore)
    self.hide()


class CustomTargetTableModel(qt.QAbstractTableModel):

  COLUMN_NAME = 'Name'
  COLUMN_2D_DISTANCE = 'Distance 2D[mm]'
  COLUMN_3D_DISTANCE = 'Distance 3D[mm]'
  COLUMN_HOLE = 'Hole'
  COLUMN_DEPTH = 'Depth [mm]'

  headers = [COLUMN_NAME, COLUMN_2D_DISTANCE, COLUMN_3D_DISTANCE, COLUMN_HOLE, COLUMN_DEPTH]

  @property
  def targetList(self):
    return self._targetList

  @targetList.setter
  def targetList(self, targetList):
    self.needleStartEndPositions = {}
    if self._targetList and self.observer:
      self._targetList.RemoveObserver(self.observer)
    self._targetList = targetList
    if self._targetList:
      self.observer = self._targetList.AddObserver(self._targetList.PointModifiedEvent, self.computeNewDepthAndHole)
    self.computeNewDepthAndHole()
    self.reset()

  @property
  def cursorPosition(self):
    return self._cursorPosition

  @cursorPosition.setter
  def cursorPosition(self, cursorPosition):
    self._cursorPosition = cursorPosition
    self.dataChanged(self.index(0, 1), self.index(self.rowCount(None)-1, 2))

  def __init__(self, logic, targets=None, parent=None, *args):
    qt.QAbstractTableModel.__init__(self, parent, *args)
    self.logic = logic
    self._cursorPosition = None
    self._targetList = None
    self.needleStartEndPositions = {}
    self.targetList = targets
    self.computeCursorDistances = False
    self.zFrameDepths = {}
    self.zFrameHole = {}
    self.observer = None
    self._targetModifiedCallback = None

  def setTargetModifiedCallback(self, func):
    assert hasattr(func, '__call__')
    self._targetModifiedCallback = func

  def headerData(self, col, orientation, role):
    if orientation == qt.Qt.Horizontal and role in [qt.Qt.DisplayRole, qt.Qt.ToolTipRole]:
        return self.headers[col]
    return None

  def rowCount(self, parent=None):
    try:
      number_of_targets = self.targetList.GetNumberOfFiducials()
      return number_of_targets
    except AttributeError:
      return 0

  def columnCount(self, parent=None):
    return len(self.headers)

  def data(self, index, role):
    if not index.isValid() or role not in [qt.Qt.DisplayRole, qt.Qt.ToolTipRole]:
      return None

    row = index.row()
    col = index.column()

    targetPosition = [0.0, 0.0, 0.0]
    if col in [1,2,3,4]:
      self.targetList.GetNthFiducialPosition(row, targetPosition)

    if col == 0:
      return self.targetList.GetNthFiducialLabel(row)
    elif (col == 1 or col == 2) and self.cursorPosition and self.computeCursorDistances:
      if col == 1:
        distance2D = self.logic.getNeedleTipTargetDistance2D(targetPosition, self.cursorPosition)
        return 'x = ' + str(round(distance2D[0], 2)) + ' y = ' + str(round(distance2D[1], 2))
      distance3D = self.logic.getNeedleTipTargetDistance3D(targetPosition, self.cursorPosition)
      return str(round(distance3D, 2))

    elif (col == 3 or col == 4) and self.logic.zFrameRegistrationSuccessful:
      if col == 3:
        return self.computeZFrameHole(row, targetPosition)
      else:
        return self.computeZFrameDepth(row, targetPosition)
    return ""

  def computeZFrameHole(self, index, targetPosition):
    if index not in self.zFrameHole.keys():
      (start, end, indexX, indexY, depth, inRange) = self.logic.computeNearestPath(targetPosition)
      self.needleStartEndPositions[index] = (start, end)
      self.zFrameHole[index] = '(%s, %s)' % (indexX, indexY)
    return self.zFrameHole[index]

  def computeZFrameDepth(self, index, targetPosition):
    if index not in self.zFrameDepths.keys():
      (start, end, indexX, indexY, depth, inRange) = self.logic.computeNearestPath(targetPosition)
      self.zFrameDepths[index] = '%.3f' % depth if inRange else '(%.3f)' % depth
    return self.zFrameDepths[index]

  def computeNewDepthAndHole(self, observer=None, caller=None):
    self.zFrameDepths = {}
    self.zFrameHole = {}
    if not self.targetList or not self.logic.zFrameRegistrationSuccessful:
      return

    for index in range(self.targetList.GetNumberOfFiducials()):
      pos = [0.0, 0.0, 0.0]
      self.targetList.GetNthFiducialPosition(index, pos)
      self.computeZFrameHole(index, pos)

    self.dataChanged(self.index(0, 3), self.index(self.rowCount(None)-1, 4))
    if self._targetModifiedCallback:
      self._targetModifiedCallback()


class SmartDICOMReceiver(ModuleLogicMixin):

  def __init__(self, incomingDataDirectory, receiveFinishedCallback):
    self.incomingDataDirectory = incomingDataDirectory
    self.receiveFinishedCallback = receiveFinishedCallback
    self.storeSCPProcess = None
    self.setupTimers()
    self.timerIterations = 0

  def setupTimers(self):
    self.dataReceivedTimer = qt.QTimer()
    self.dataReceivedTimer.setInterval(5000)
    self.dataReceivedTimer.timeout.connect(self.checkIfStillSameFileCount)
    self.dataReceivedTimer.setSingleShot(True)

    self.watchTimer = qt.QTimer()
    self.watchTimer.setInterval(1000)
    self.watchTimer.timeout.connect(self.startWatching)
    self.watchTimer.setSingleShot(True)

  def start(self):
    self.stop()

    self.dataHasBeenReceived = False
    self.lastFileCount = len(self.getFileList(self.incomingDataDirectory))

    self.storeSCPProcess = DICOMLib.DICOMStoreSCPProcess(incomingDataDir=self.incomingDataDirectory)
    self.startWatching()
    self.storeSCPProcess.start()
    slicer.util.showStatusMessage("Waiting for incoming DICOM data")

  def stop(self):
    self.stopWatching()
    if self.storeSCPProcess:
      self.storeSCPProcess.stop()

  def startWatching(self):
    self.currentFileCount = len(self.getFileList(self.incomingDataDirectory))
    if self.lastFileCount != self.currentFileCount:
      slicer.util.showStatusMessage(self.getReceivingStatusMessage())
      self.dataHasBeenReceived = True
      self.lastFileCount = self.currentFileCount
      self.watchTimer.start()
    elif self.dataHasBeenReceived:
      self.lastFileCount = self.currentFileCount
      slicer.util.showStatusMessage("DICOM data receive completed.")
      self.dataReceivedTimer.start()
    else:
      self.watchTimer.start()

  def getReceivingStatusMessage(self):
    if self.timerIterations == 4:
      self.timerIterations = 0
    dots = ""
    for iteration in range(self.timerIterations):
      dots += "."
    self.timerIterations += 1
    return "Receiving DICOM data %s" % dots

  def stopWatching(self):
    self.dataReceivedTimer.stop()
    self.watchTimer.stop()

  def checkIfStillSameFileCount(self):
    self.currentFileCount = len(self.getFileList(self.incomingDataDirectory))
    if self.lastFileCount == self.currentFileCount:
      self.receiveFinishedCallback()
    else:
      self.watchTimer.start()