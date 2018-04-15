"""

TODO:
  - Sorting by standard deviation: Use coefficient of variation (std/mean)
    or quartile coefficient of dispersion (Q3 - Q1) / (Q3 + Q1)
  - Standard deviation for nominal: try out Variation ratio (1 - n_mode/N)
"""
import locale
from enum import IntEnum
from typing import Any, Optional, Tuple, List  # pylint: disable=unused-import

import numpy as np
import scipy.stats as ss
from AnyQt.QtCore import Qt, QSize, QRectF, QVariant, QModelIndex, pyqtSlot, \
    QRegExp, QItemSelection, QItemSelectionRange, QItemSelectionModel
from AnyQt.QtGui import QPainter, QColor
from AnyQt.QtWidgets import QStyleOptionViewItem
from AnyQt.QtWidgets import QStyledItemDelegate, QGraphicsScene, QTableView, \
    QHeaderView, QStyle

import Orange.statistics.util as ut
from Orange.canvas.report import plural
from Orange.data import Table, StringVariable, DiscreteVariable, \
    ContinuousVariable, TimeVariable, Domain, Variable
from Orange.widgets import widget, gui
from Orange.widgets.settings import ContextSetting, DomainContextHandler, \
    Setting
from Orange.widgets.utils.itemmodels import DomainModel, AbstractSortTableModel
from Orange.widgets.utils.signals import Input, Output
from orangecontrib.prototypes.widgets.utils.histogram import Histogram


def _categorical_entropy(x):
    """Compute the entropy of a dense/sparse matrix, column-wise. Assuming
    categorical values."""
    p = [ut.bincount(row)[0] for row in x.T]
    p = [pk / np.sum(pk) for pk in p]
    return np.fromiter((ss.entropy(pk) for pk in p), dtype=np.float64)


class FeatureStatisticsTableModel(AbstractSortTableModel):
    CLASS_VAR, META, ATTRIBUTE = range(3)
    COLOR_FOR_ROLE = {
        CLASS_VAR: QColor(160, 160, 160),
        META: QColor(220, 220, 200),
        ATTRIBUTE: QColor(255, 255, 255),
    }

    HIDDEN_VAR_TYPES = (StringVariable, TimeVariable)

    class Columns(IntEnum):
        ICON, NAME, DISTRIBUTION, CENTER, DISPERSION, MIN, MAX, MISSING = range(8)

        @property
        def name(self):
            return {self.ICON: '',
                    self.NAME: 'Name',
                    self.DISTRIBUTION: 'Distribution',
                    self.CENTER: 'Center',
                    self.DISPERSION: 'Dispersion',
                    self.MIN: 'Min.',
                    self.MAX: 'Max.',
                    self.MISSING: 'Missing',
                    }[self.value]

        @property
        def index(self):
            return self.value

        @classmethod
        def from_index(cls, index):
            return cls(index)

    def __init__(self, data=None, parent=None):
        """

        Parameters
        ----------
        data : Optional[Table]
        parent : Optional[QWidget]

        """
        super().__init__(parent)

        self.table = None  # type: Optional[Table]
        self.domain = None  # type: Optional[Domain]
        self.target_var = None  # type: Optional[Variable]
        self.n_attributes = self.n_instances = 0

        self.__attributes = self.__class_vars = self.__metas = None
        self.__distributions_cache = {}
        # Clear model initially to set default values
        self.clear()

        self.set_data(data)

    def set_data(self, data):
        if data is None:
            self.clear()
            return

        self.beginResetModel()
        self.table = data
        self.domain = domain = data.domain
        self.target_var = None

        self.__attributes = self.__filter_attributes(domain.attributes, self.table.X)
        self.__class_vars = self.__filter_attributes(domain.class_vars, self.table._Y)
        self.__metas = self.__filter_attributes(domain.metas, self.table.metas)

        self.n_attributes = len(self.variables)
        self.n_instances = len(data)

        self.__distributions_cache = {}
        self.__compute_statistics()
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.table = self.domain = self.target_var = None
        self.n_attributes = self.n_instances = 0
        self.__attributes = (np.array([]), np.array([]))
        self.__class_vars = (np.array([]), np.array([]))
        self.__metas = (np.array([]), np.array([]))
        self.__distributions_cache.clear()
        self.endResetModel()

    @property
    def variables(self):
        matrices = [self.__attributes[0], self.__class_vars[0], self.__metas[0]]
        if not any(m.size for m in matrices):
            return []
        return np.hstack(matrices)

    @staticmethod
    def _attr_indices(attrs):
        # type: (List) -> Tuple[List[int], List[int], List[int], List[int]]
        """Get the indices of different attribute types eg. discrete."""
        disc_var_idx = [i for i, attr in enumerate(attrs) if isinstance(attr, DiscreteVariable)]
        cont_var_idx = [i for i, attr in enumerate(attrs)
                        if isinstance(attr, ContinuousVariable)
                        and not isinstance(attr, TimeVariable)]
        time_var_idx = [i for i, attr in enumerate(attrs) if isinstance(attr, TimeVariable)]
        string_var_idx = [i for i, attr in enumerate(attrs) if isinstance(attr, StringVariable)]
        return disc_var_idx, cont_var_idx, time_var_idx, string_var_idx

    def __filter_attributes(self, attributes, matrix):
        """Filter out variables which shouldn't be visualized."""
        attributes, matrix = np.asarray(attributes), matrix
        mask = [idx for idx, attr in enumerate(attributes)
                if not isinstance(attr, self.HIDDEN_VAR_TYPES)]
        return attributes[mask], matrix[:, mask]

    def __compute_statistics(self):
        # Since data matrices can of mixed sparsity, we need to compute
        # attributes separately for each of them.
        matrices = [self.__attributes, self.__class_vars, self.__metas]
        # Filter out any matrices with size 0
        matrices = list(filter(lambda tup: tup[1].size, matrices))

        self._variable_types = [type(var).__name__ for var in self.variables]
        self._variable_names = [var.name.lower() for var in self.variables]
        self._center = self.__compute_stat(
            matrices,
            discrete_f=lambda x: ss.mode(x)[0],
            continuous_f=lambda x: ut.nanmean(x, axis=0),
        )
        self._dispersion = self.__compute_stat(
            matrices,
            discrete_f=_categorical_entropy,
            continuous_f=lambda x: np.sqrt(ut.nanvar(x, axis=0)) / ut.nanmean(x, axis=0),
        )
        self._min = self.__compute_stat(
            matrices,
            discrete_f=lambda x: ut.nanmin(x, axis=0),
            continuous_f=lambda x: ut.nanmin(x, axis=0),
        )
        self._max = self.__compute_stat(
            matrices,
            discrete_f=lambda x: ut.nanmax(x, axis=0),
            continuous_f=lambda x: ut.nanmax(x, axis=0),
        )
        self._missing = self.__compute_stat(
            matrices,
            discrete_f=lambda x: ut.countnans(x, axis=0),
            continuous_f=lambda x: ut.countnans(x, axis=0),
            string_f=lambda x: (x == StringVariable.Unknown).sum(axis=0),
            time_f=lambda x: ut.countnans(x, axis=0),
        )

    def get_statistics_matrix(self, variables=None, return_labels=False):
        """Get the numeric computed statistics in a single matrix. Optionally,
        we can specify for which variables we want the stats. Also, we can get
        the string column names as labels if desired.

        Parameters
        ----------
        variables : Iterable[Union[Variable, int, str]]
            Return statistics for only the variables specified. Accepts all
            formats supported by `domain.index`
        return_labels : bool
            In addition to the statistics matrix, also return string labels for
            the columns of the matrix e.g. 'Mean' or 'Dispersion', as specified
            in `Columns`.

        Returns
        -------
        Union[Tuple[List[str], np.ndarray], np.ndarray]

        """
        if self.table is None:
            return np.atleast_2d([])

        # If a list of variables is given, select only corresponding stats
        if variables is not None and len(variables):
            indices = [self.domain.index(var) for var in variables]
        else:
            indices = ...

        matrix = np.vstack((
            self._center[indices], self._dispersion[indices],
            self._min[indices], self._max[indices], self._missing[indices],
        )).T

        # Return string labels for the returned matrix columns e.g. 'Mean',
        # 'Dispersion' if requested
        if return_labels:
            labels = [self.Columns.CENTER.name, self.Columns.DISPERSION.name,
                      self.Columns.MIN.name, self.Columns.MAX.name,
                      self.Columns.MISSING.name]
            return labels, matrix

        return matrix

    def __compute_stat(self, matrices, discrete_f=None, continuous_f=None,
                       time_f=None, string_f=None, default_val=np.nan):
        """Apply functions to appropriate variable types. The default value is
        returned if there is no function defined for specific variable types.
        """
        if not len(matrices):
            return np.array([])

        def _to_float(data):
            if not np.issubdtype(data.dtype, np.number):
                data = data.astype(np.float64)
            return data

        def _to_object(data):
            if data.dtype is not np.object:
                data = data.astype(np.object)
            return data

        results = []
        for variables, x in matrices:
            result = np.full(len(variables), default_val)

            # While the following caching and checks are messy, the indexing
            # turns out to be a bottleneck for large datasets, so a single
            # indexing operation improves performance
            disc_idx, cont_idx, time_idx, str_idx = self._attr_indices(variables)
            if discrete_f:
                x_ = x[:, disc_idx]
                if x_.size:
                    result[disc_idx] = discrete_f(_to_float(x_))
            if continuous_f:
                x_ = x[:, cont_idx]
                if x_.size:
                    result[cont_idx] = continuous_f(_to_float(x_))
            if time_f:
                x_ = x[:, time_idx]
                if x_.size:
                    result[time_idx] = time_f(_to_float(x_))
            if string_f:
                x_ = x[:, str_idx]
                if x_.size:
                    result[str_idx] = string_f(_to_object(x_))

            results.append(result)

        return np.hstack(results)

    def sortColumnData(self, column):
        if column == self.Columns.ICON:
            return self._variable_types
        elif column == self.Columns.NAME:
            return self._variable_names
        elif column == self.Columns.DISTRIBUTION:
            return self._variable_names
        elif column == self.Columns.CENTER:
            return self._center
        elif column == self.Columns.DISPERSION:
            return self._dispersion
        elif column == self.Columns.MIN:
            return self._min
        elif column == self.Columns.MAX:
            return self._max
        elif column == self.Columns.MISSING:
            return self._missing

    def _argsortData(self, data, order):
        """Always sort NaNs last."""
        indices = np.argsort(data, kind='mergesort')
        if order == Qt.DescendingOrder:
            indices = indices[::-1]
            if np.issubdtype(data.dtype, np.number):
                indices = np.roll(indices, -np.isnan(data).sum())
            return indices
        return indices

    def headerData(self, section, orientation, role):
        # type: (int, Qt.Orientation, Qt.ItemDataRole) -> Any
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.Columns.from_index(section).name

    def data(self, index, role):
        # type: (QModelIndex, Qt.ItemDataRole) -> Any
        if not index.isValid():
            return

        row, column = self.mapToSourceRows(index.row()), index.column()
        # Make sure we're not out of range
        if not 0 <= row <= self.n_attributes:
            return QVariant()

        attribute = self.variables[row]

        if role == Qt.BackgroundRole:
            if attribute in self.domain.attributes:
                return self.COLOR_FOR_ROLE[self.ATTRIBUTE]
            elif attribute in self.domain.metas:
                return self.COLOR_FOR_ROLE[self.META]
            elif attribute in self.domain.class_vars:
                return self.COLOR_FOR_ROLE[self.CLASS_VAR]

        elif role == Qt.TextAlignmentRole:
            if column == self.Columns.NAME:
                return Qt.AlignLeft | Qt.AlignVCenter
            return Qt.AlignRight | Qt.AlignVCenter

        output = None

        if column == self.Columns.ICON:
            if role == Qt.DecorationRole:
                return gui.attributeIconDict[attribute]
        elif column == self.Columns.NAME:
            if role == Qt.DisplayRole:
                output = attribute.name
        elif column == self.Columns.DISTRIBUTION:
            if role == Qt.DisplayRole:
                if isinstance(attribute, (DiscreteVariable, ContinuousVariable)):
                    if row not in self.__distributions_cache:
                        scene = QGraphicsScene(parent=self)
                        histogram = Histogram(
                            data=self.table,
                            variable=attribute,
                            color_attribute=self.target_var,
                            border=(0, 0, 2, 0),
                            border_color='#ccc',
                        )
                        scene.addItem(histogram)
                        self.__distributions_cache[row] = scene
                    return self.__distributions_cache[row]
        elif column == self.Columns.CENTER:
            if role == Qt.DisplayRole:
                if isinstance(attribute, DiscreteVariable):
                    output = self._center[row]
                    if not np.isnan(output):
                        output = attribute.str_val(self._center[row])
                else:
                    output = self._center[row]
        elif column == self.Columns.DISPERSION:
            if role == Qt.DisplayRole:
                output = self._dispersion[row]
        elif column == self.Columns.MIN:
            if role == Qt.DisplayRole:
                if isinstance(attribute, DiscreteVariable):
                    if attribute.ordered:
                        output = attribute.str_val(self._min[row])
                else:
                    output = self._min[row]
        elif column == self.Columns.MAX:
            if role == Qt.DisplayRole:
                if isinstance(attribute, DiscreteVariable):
                    if attribute.ordered:
                        output = attribute.str_val(self._max[row])
                else:
                    output = self._max[row]
        elif column == self.Columns.MISSING:
            if role == Qt.DisplayRole:
                output = '%d (%d%%)' % (
                    self._missing[row],
                    100 * self._missing[row] / self.n_instances
                )

        # Consistently format the text inside the table cells
        # The easiest way to check for NaN is to compare with itself
        if output != output:
            output = 'NaN'
        # Format ∞ properly
        elif output in (np.inf, -np.inf):
            output = '%s∞' % ['', '-'][output < 0]
        elif isinstance(output, int):
            output = locale.format('%d', output, grouping=True)
        elif isinstance(output, float):
            output = locale.format('%.2f', output, grouping=True)

        return output

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.n_attributes

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.Columns)

    def set_target_var(self, variable):
        self.target_var = variable
        self.__distributions_cache.clear()
        start_idx = self.index(0, self.Columns.DISTRIBUTION)
        end_idx = self.index(self.rowCount(), self.Columns.DISTRIBUTION)
        self.dataChanged.emit(start_idx, end_idx)


class FeatureStatisticsTableView(QTableView):
    HISTOGRAM_ASPECT_RATIO = (7, 3)
    MINIMUM_HISTOGRAM_HEIGHT = 50
    MAXIMUM_HISTOGRAM_HEIGHT = 80

    def __init__(self, model, parent=None, **kwargs):
        super().__init__(
            parent=parent,
            showGrid=False,
            cornerButtonEnabled=False,
            sortingEnabled=True,
            selectionBehavior=QTableView.SelectRows,
            selectionMode=QTableView.ExtendedSelection,
            horizontalScrollMode=QTableView.ScrollPerPixel,
            verticalScrollMode=QTableView.ScrollPerPixel,
            **kwargs
        )
        self.setModel(model)

        hheader = self.horizontalHeader()
        hheader.setStretchLastSection(False)
        # Contents precision specifies how many rows should be taken into
        # account when computing the sizes, 0 being the visible rows. This is
        # crucial, since otherwise the `ResizeToContents` section resize mode
        # would call `sizeHint` on every single row in the data before first
        # render. However this, this cannot be used here, since this only
        # appears to work properly when the widget is actually shown. When the
        # widget is not shown, size `sizeHint` is called on every row.
        hheader.setResizeContentsPrecision(5)
        # Set a nice default size so that headers have some space around titles
        hheader.setDefaultSectionSize(100)
        # Set individual column behaviour in `set_data` since the logical
        # indices must be valid in the model, which requires data.
        hheader.setSectionResizeMode(QHeaderView.Interactive)

        columns = model.Columns
        hheader.setSectionResizeMode(columns.ICON.index, QHeaderView.ResizeToContents)
        hheader.setSectionResizeMode(columns.DISTRIBUTION.index, QHeaderView.Stretch)

        vheader = self.verticalHeader()
        vheader.setVisible(False)
        vheader.setSectionResizeMode(QHeaderView.Fixed)
        hheader.sectionResized.connect(self.bind_histogram_aspect_ratio)
        # TODO: This shifts the scrollarea a bit down when opening widget
        # hheader.sectionResized.connect(self.keep_row_centered)

        self.setItemDelegate(NoFocusRectDelegate(parent=self))
        self.setItemDelegateForColumn(
            FeatureStatisticsTableModel.Columns.DISTRIBUTION,
            DistributionDelegate(parent=self),
        )

    def bind_histogram_aspect_ratio(self, logical_index, _, new_size):
        """Force the horizontal and vertical header to maintain the defined
        aspect ratio specified for the histogram."""
        # Prevent function being exectued more than once per resize
        if logical_index is not self.model().Columns.DISTRIBUTION.index:
            return
        ratio_width, ratio_height = self.HISTOGRAM_ASPECT_RATIO
        unit_width = new_size / ratio_width
        new_height = unit_width * ratio_height
        effective_height = max(new_height, self.MINIMUM_HISTOGRAM_HEIGHT)
        effective_height = min(effective_height, self.MAXIMUM_HISTOGRAM_HEIGHT)
        self.verticalHeader().setDefaultSectionSize(effective_height)

    def keep_row_centered(self, logical_index, old_size, new_size):
        """When resizing the widget when scrolled further down, the
        positions of rows changes. Obviously, the user resized in order to
        better see the row of interest. This keeps that row centered."""
        # TODO: This does not work properly
        # Prevent function being exectued more than once per resize
        if logical_index is not self.model().Columns.DISTRIBUTION.index:
            return
        top_row = self.indexAt(self.rect().topLeft()).row()
        bottom_row = self.indexAt(self.rect().bottomLeft()).row()
        middle_row = top_row + (bottom_row - top_row) // 2
        self.scrollTo(self.model().index(middle_row, 0), QTableView.PositionAtCenter)


class NoFocusRectDelegate(QStyledItemDelegate):
    """Removes the light blue background and border on a focused item."""

    def paint(self, painter, option, index):
        # type: (QPainter, QStyleOptionViewItem, QModelIndex) -> None
        option.state &= ~QStyle.State_HasFocus
        super().paint(painter, option, index)


class DistributionDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # type: (QPainter, QStyleOptionViewItem, QModelIndex) -> None
        scene = index.data(Qt.DisplayRole)  # type: Optional[QGraphicsScene]
        if scene is None:
            return super().paint(painter, option, index)

        painter.setRenderHint(QPainter.Antialiasing)

        if option.state & QStyle.State_Selected:
            background_color = option.palette.highlight()
        else:
            background_color = index.data(Qt.BackgroundRole)
        if background_color is not None:
            scene.setBackgroundBrush(background_color)

        scene.render(painter, target=QRectF(option.rect), mode=Qt.IgnoreAspectRatio)


class OWFeatureStatistics(widget.OWWidget):
    name = 'Feature Statistics'
    description = 'Show basic statistics for data features.'
    icon = 'icons/FeatureStatistics.svg'

    class Inputs:
        data = Input('Data', Table, default=True)

    class Outputs:
        reduced_data = Output('Reduced Data', Table, default=True)
        statistics = Output('Statistics', Table)

    want_main_area = True
    buttons_area_orientation = Qt.Vertical

    settingsHandler = DomainContextHandler()

    auto_commit = ContextSetting(True)
    color_var = ContextSetting(None)  # type: Optional[Variable]
    # filter_string = ContextSetting('')

    sorting = ContextSetting((0, Qt.DescendingOrder))
    selected_rows = ContextSetting([])

    def __init__(self):
        super().__init__()

        self.data = None  # type: Optional[Table]

        # Information panel
        info_box = gui.vBox(self.controlArea, 'Info')
        info_box.setMinimumWidth(200)
        self.info_summary = gui.widgetLabel(info_box, wordWrap=True)
        self.info_attr = gui.widgetLabel(info_box, wordWrap=True)
        self.info_class = gui.widgetLabel(info_box, wordWrap=True)
        self.info_meta = gui.widgetLabel(info_box, wordWrap=True)
        self.set_info()

        # TODO: Implement filtering on the model
        # filter_box = gui.vBox(self.controlArea, 'Filter')
        # self.filter_text = gui.lineEdit(
        #     filter_box, self, value='filter_string',
        #     placeholderText='Filter variables by name',
        #     callback=self._filter_table_variables, callbackOnType=True,
        # )
        # shortcut = QShortcut(QKeySequence('Ctrl+f'), self, self.filter_text.setFocus)
        # shortcut.setWhatsThis('Filter variables by name')

        self.color_var_model = DomainModel(
            valid_types=(ContinuousVariable, DiscreteVariable),
            placeholder='None',
        )
        box = gui.vBox(self.controlArea, 'Histogram')
        self.cb_color_var = gui.comboBox(
            box, master=self, value='color_var', model=self.color_var_model,
            label='Color:', orientation=Qt.Horizontal,
        )
        self.cb_color_var.activated.connect(self.__color_var_changed)

        gui.rubber(self.controlArea)
        gui.auto_commit(
            self.buttonsArea, self, 'auto_commit', 'Send Selected Rows',
            'Send Automatically',
        )

        # Main area
        self.model = FeatureStatisticsTableModel(parent=self)
        self.table_view = FeatureStatisticsTableView(self.model, parent=self)
        self.table_view.selectionModel().selectionChanged.connect(self.on_select)
        self.table_view.horizontalHeader().sectionClicked.connect(self.on_header_click)

        self.mainArea.layout().addWidget(self.table_view)

    def sizeHint(self):
        return QSize(1050, 500)

    def _filter_table_variables(self):
        regex = QRegExp(self.filter_string)
        # If the user explicitly types different cases, we assume they know
        # what they are searching for and account for letter case in filter
        different_case = (
            any(c.islower() for c in self.filter_string) and
            any(c.isupper() for c in self.filter_string)
        )
        if not different_case:
            regex.setCaseSensitivity(Qt.CaseInsensitive)

    @Inputs.data
    def set_data(self, data):
        self.closeContext()
        self.selected_rows = []
        self.model.resetSorting()

        self.data = data

        if data is not None:
            self.color_var_model.set_domain(data.domain)
            if self.data.domain.class_vars:
                self.color_var = self.data.domain.class_vars[0]
        else:
            self.color_var_model.set_domain(None)
            self.color_var = None
        self.model.set_data(data)

        self.openContext(self.data)
        self.__restore_selection()
        self.__restore_sorting()
        # self._filter_table_variables()
        self.__color_var_changed()

        self.set_info()

    def __restore_selection(self):
        """Restore the selection on the table view from saved settings."""
        selection_model = self.table_view.selectionModel()
        selection = QItemSelection()
        if len(self.selected_rows):
            for row in self.model.mapFromSourceRows(self.selected_rows):
                selection.append(QItemSelectionRange(
                    self.model.index(row, 0),
                    self.model.index(row, self.model.columnCount() - 1)
                ))
        selection_model.select(selection, QItemSelectionModel.ClearAndSelect)

    def __restore_sorting(self):
        """Restore the sort column and order from saved settings."""
        sort_column, sort_order = self.sorting
        if sort_column < self.model.columnCount():
            self.model.sort(sort_column, sort_order)
            self.table_view.horizontalHeader().setSortIndicator(sort_column, sort_order)

    @pyqtSlot(int)
    def on_header_click(self, *_):
        # Store the header states
        sort_order = self.model.sortOrder()
        sort_column = self.model.sortColumn()
        self.sorting = sort_column, sort_order

    @pyqtSlot(int)
    def __color_var_changed(self, *_):
        if self.model is not None:
            self.model.set_target_var(self.color_var)

    def _format_variables_string(self, variables):
        agg = []
        for var_type_name, var_type in [
            ('categorical', DiscreteVariable),
            ('numeric', ContinuousVariable),
            ('time', TimeVariable),
            ('string', StringVariable)
        ]:
            var_type_list = [v for v in variables if type(v) is var_type]
            if var_type_list:
                shown = var_type in self.model.HIDDEN_VAR_TYPES
                agg.append((
                    '%d %s%s' % (len(var_type_list), var_type_name, ['', ' (not shown)'][shown]),
                    len(var_type_list)
                ))

        if not agg:
            return 'No variables'

        attrs, counts = list(zip(*agg))
        if len(attrs) > 1:
            var_string = ', '.join(attrs[:-1]) + ' and ' + attrs[-1]
        else:
            var_string = attrs[0]
        return plural('%s variable{s}' % var_string, sum(counts))

    def set_info(self):
        if self.data is not None:
            self.info_summary.setText('<b>%s</b> contains %s with %s' % (
                self.data.name,
                plural('{number} instance{s}', self.model.n_instances),
                plural('{number} feature{s}', self.model.n_attributes)
            ))

            self.info_attr.setText(
                '<b>Attributes:</b><br>%s' %
                self._format_variables_string(self.data.domain.attributes)
            )
            self.info_class.setText(
                '<b>Class variables:</b><br>%s' %
                self._format_variables_string(self.data.domain.class_vars)
            )
            self.info_meta.setText(
                '<b>Metas:</b><br>%s' %
                self._format_variables_string(self.data.domain.metas)
            )
        else:
            self.info_summary.setText('No data on input.')
            self.info_attr.setText('')
            self.info_class.setText('')
            self.info_meta.setText('')

    def on_select(self):
        self.selected_rows = self.model.mapToSourceRows([
            i.row() for i in self.table_view.selectionModel().selectedRows()
        ])
        self.commit()

    def commit(self):
        if not len(self.selected_rows):
            self.Outputs.reduced_data.send(None)
            self.Outputs.statistics.send(None)
            return

        # Send a table with only selected columns to output
        variables = self.model.variables[self.selected_rows]
        self.Outputs.reduced_data.send(self.data[:, variables])

        # Send the statistics of the selected variables to ouput
        labels, data = self.model.get_statistics_matrix(variables, return_labels=True)
        var_names = np.atleast_2d([var.name for var in variables]).T
        domain = Domain(
            attributes=[ContinuousVariable(name) for name in labels],
            metas=[StringVariable('Feature')]
        )
        statistics = Table(domain, data, metas=var_names)
        statistics.name = '%s (Feature Statistics)' % self.data.name
        self.Outputs.statistics.send(statistics)

    def send_report(self):
        pass


if __name__ == '__main__':
    from AnyQt.QtWidgets import QApplication
    import sys

    app = QApplication(sys.argv)
    ow = OWFeatureStatistics()

    ow.set_data(Table(sys.argv[1] if len(sys.argv) > 1 else 'iris'))
    ow.show()
    app.exec_()
