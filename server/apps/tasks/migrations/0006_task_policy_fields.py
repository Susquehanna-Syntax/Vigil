from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0005_task_step_label_task_step_order_alter_task_state_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='schedule',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='task',
            name='retry_count',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='task',
            name='max_retries',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='task',
            name='retry_delay_seconds',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='task',
            name='not_before',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
