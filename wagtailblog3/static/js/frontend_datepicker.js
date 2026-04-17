$(document).ready(function() {

    // 初始化开始日期选择器

    const startDatePicker = $("#start-date").flatpickr({

        locale: "zh",

        dateFormat: "Y-m-d",

        maxDate: new Date(),

        onChange: function(selectedDates, dateStr, instance) {

            // 当选择开始日期后，设置结束日期的最小日期为开始日期

            // 确保结束日期不能早于开始日期

            if (selectedDates[0]) {

                endDatePicker.set("minDate", selectedDates[0]);

                

                // 如果当前结束日期早于新选择的开始日期，清空结束日期

                const currentEndDate = endDatePicker.selectedDates[0];

                if (currentEndDate && currentEndDate < selectedDates[0]) {

                    endDatePicker.clear();

                }

            }

        }

    });



    // 初始化结束日期选择器

    const endDatePicker = $("#end-date").flatpickr({

        locale: "zh",

        dateFormat: "Y-m-d",

        maxDate: new Date(),

        onChange: function(selectedDates, dateStr, instance) {

            // 当选择结束日期后，设置开始日期的最大日期为结束日期

            // 确保开始日期不能晚于结束日期

            if (selectedDates[0]) {

                startDatePicker.set("maxDate", selectedDates[0]);

                

                // 如果当前开始日期晚于新选择的结束日期，清空开始日期

                const currentStartDate = startDatePicker.selectedDates[0];

                if (currentStartDate && currentStartDate > selectedDates[0]) {

                    startDatePicker.clear();

                }

            }

        }

    });

});